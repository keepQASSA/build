# Copyright (C) 2008 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import base64
import collections
import copy
import errno
import fnmatch
import getopt
import getpass
import gzip
import imp
import json
import logging
import logging.config
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from hashlib import sha1, sha256

import blockimgdiff
import filesystemdiff
import sparse_img

logger = logging.getLogger(__name__)


class Options(object):
  def __init__(self):
    base_out_path = os.getenv('OUT_DIR_COMMON_BASE')
    if base_out_path is None:
      base_search_path = "out"
    else:
      base_search_path = os.path.join(base_out_path,
                                      os.path.basename(os.getcwd()))

    # Python >= 3.3 returns 'linux', whereas Python 2.7 gives 'linux2'.
    platform_search_path = {
        "linux": os.path.join(base_search_path, "host/linux-x86"),
        "linux2": os.path.join(base_search_path, "host/linux-x86"),
        "darwin": os.path.join(base_search_path, "host/darwin-x86"),
    }

    self.search_path = platform_search_path.get(sys.platform)
    self.signapk_path = "framework/signapk.jar"  # Relative to search_path
    self.signapk_shared_library_path = "lib64"   # Relative to search_path
    self.extra_signapk_args = []
    self.java_path = "java"  # Use the one on the path by default.
    self.java_args = ["-Xmx2048m"]  # The default JVM args.
    self.public_key_suffix = ".x509.pem"
    self.private_key_suffix = ".pk8"
    # use otatools built boot_signer by default
    self.boot_signer_path = "boot_signer"
    self.boot_signer_args = []
    self.verity_signer_path = None
    self.verity_signer_args = []
    self.verbose = False
    self.tempfiles = []
    self.device_specific = None
    self.extras = {}
    self.info_dict = None
    self.source_info_dict = None
    self.target_info_dict = None
    self.worker_threads = None
    # Stash size cannot exceed cache_size * threshold.
    self.cache_size = None
    self.stash_threshold = 0.8


OPTIONS = Options()

# The block size that's used across the releasetools scripts.
BLOCK_SIZE = 4096

# Values for "certificate" in apkcerts that mean special things.
SPECIAL_CERT_STRINGS = ("PRESIGNED", "EXTERNAL")

# The partitions allowed to be signed by AVB (Android verified boot 2.0).
AVB_PARTITIONS = ('boot', 'recovery', 'system', 'vendor', 'product',
                  'product_services', 'dtbo', 'odm')

# Chained VBMeta partitions.
AVB_VBMETA_PARTITIONS = ('vbmeta_system', 'vbmeta_vendor')

# Partitions that should have their care_map added to META/care_map.pb
PARTITIONS_WITH_CARE_MAP = ('system', 'vendor', 'product', 'product_services',
                            'odm')


class ErrorCode(object):
  """Define error_codes for failures that happen during the actual
  update package installation.

  Error codes 0-999 are reserved for failures before the package
  installation (i.e. low battery, package verification failure).
  Detailed code in 'bootable/recovery/error_code.h' """

  SYSTEM_VERIFICATION_FAILURE = 1000
  SYSTEM_UPDATE_FAILURE = 1001
  SYSTEM_UNEXPECTED_CONTENTS = 1002
  SYSTEM_NONZERO_CONTENTS = 1003
  SYSTEM_RECOVER_FAILURE = 1004
  VENDOR_VERIFICATION_FAILURE = 2000
  VENDOR_UPDATE_FAILURE = 2001
  VENDOR_UNEXPECTED_CONTENTS = 2002
  VENDOR_NONZERO_CONTENTS = 2003
  VENDOR_RECOVER_FAILURE = 2004
  OEM_PROP_MISMATCH = 3000
  FINGERPRINT_MISMATCH = 3001
  THUMBPRINT_MISMATCH = 3002
  OLDER_BUILD = 3003
  DEVICE_MISMATCH = 3004
  BAD_PATCH_FILE = 3005
  INSUFFICIENT_CACHE_SPACE = 3006
  TUNE_PARTITION_FAILURE = 3007
  APPLY_PATCH_FAILURE = 3008


class ExternalError(RuntimeError):
  pass


def InitLogging():
  DEFAULT_LOGGING_CONFIG = {
      'version': 1,
      'disable_existing_loggers': False,
      'formatters': {
          'standard': {
              'format':
                  '%(asctime)s - %(filename)s - %(levelname)-8s: %(message)s',
              'datefmt': '%Y-%m-%d %H:%M:%S',
          },
      },
      'handlers': {
          'default': {
              'class': 'logging.StreamHandler',
              'formatter': 'standard',
          },
      },
      'loggers': {
          '': {
              'handlers': ['default'],
              'level': 'WARNING',
              'propagate': True,
          }
      }
  }
  env_config = os.getenv('LOGGING_CONFIG')
  if env_config:
    with open(env_config) as f:
      config = json.load(f)
  else:
    config = DEFAULT_LOGGING_CONFIG

    # Increase the logging level for verbose mode.
    if OPTIONS.verbose:
      config = copy.deepcopy(DEFAULT_LOGGING_CONFIG)
      config['loggers']['']['level'] = 'INFO'

  logging.config.dictConfig(config)


def Run(args, verbose=None, **kwargs):
  """Creates and returns a subprocess.Popen object.

  Args:
    args: The command represented as a list of strings.
    verbose: Whether the commands should be shown. Default to the global
        verbosity if unspecified.
    kwargs: Any additional args to be passed to subprocess.Popen(), such as env,
        stdin, etc. stdout and stderr will default to subprocess.PIPE and
        subprocess.STDOUT respectively unless caller specifies any of them.
        universal_newlines will default to True, as most of the users in
        releasetools expect string output.

  Returns:
    A subprocess.Popen object.
  """
  if 'stdout' not in kwargs and 'stderr' not in kwargs:
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.STDOUT
  if 'universal_newlines' not in kwargs:
    kwargs['universal_newlines'] = True
  # Don't log any if caller explicitly says so.
  if verbose != False:
    logger.info("  Running: \"%s\"", " ".join(args))
  return subprocess.Popen(args, **kwargs)


def RunAndWait(args, verbose=None, **kwargs):
  """Runs the given command waiting for it to complete.

  Args:
    args: The command represented as a list of strings.
    verbose: Whether the commands should be shown. Default to the global
        verbosity if unspecified.
    kwargs: Any additional args to be passed to subprocess.Popen(), such as env,
        stdin, etc. stdout and stderr will default to subprocess.PIPE and
        subprocess.STDOUT respectively unless caller specifies any of them.

  Raises:
    ExternalError: On non-zero exit from the command.
  """
  proc = Run(args, verbose=verbose, **kwargs)
  proc.wait()

  if proc.returncode != 0:
    raise ExternalError(
        "Failed to run command '{}' (exit code {})".format(
            args, proc.returncode))


def RunAndCheckOutput(args, verbose=None, **kwargs):
  """Runs the given command and returns the output.

  Args:
    args: The command represented as a list of strings.
    verbose: Whether the commands should be shown. Default to the global
        verbosity if unspecified.
    kwargs: Any additional args to be passed to subprocess.Popen(), such as env,
        stdin, etc. stdout and stderr will default to subprocess.PIPE and
        subprocess.STDOUT respectively unless caller specifies any of them.

  Returns:
    The output string.

  Raises:
    ExternalError: On non-zero exit from the command.
  """
  proc = Run(args, verbose=verbose, **kwargs)
  output, _ = proc.communicate()
  # Don't log any if caller explicitly says so.
  if verbose != False:
    logger.info("%s", output.rstrip())
  if proc.returncode != 0:
    raise ExternalError(
        "Failed to run command '{}' (exit code {}):\n{}".format(
            args, proc.returncode, output))
  return output


def RoundUpTo4K(value):
  rounded_up = value + 4095
  return rounded_up - (rounded_up % 4096)


def CloseInheritedPipes():
  """ Gmake in MAC OS has file descriptor (PIPE) leak. We close those fds
  before doing other work."""
  if platform.system() != "Darwin":
    return
  for d in range(3, 1025):
    try:
      stat = os.fstat(d)
      if stat is not None:
        pipebit = stat[0] & 0x1000
        if pipebit != 0:
          os.close(d)
    except OSError:
      pass


def LoadInfoDict(input_file, repacking=False):
  """Loads the key/value pairs from the given input target_files.

  It reads `META/misc_info.txt` file in the target_files input, does sanity
  checks and returns the parsed key/value pairs for to the given build. It's
  usually called early when working on input target_files files, e.g. when
  generating OTAs, or signing builds. Note that the function may be called
  against an old target_files file (i.e. from past dessert releases). So the
  property parsing needs to be backward compatible.

  In a `META/misc_info.txt`, a few properties are stored as links to the files
  in the PRODUCT_OUT directory. It works fine with the build system. However,
  they are no longer available when (re)generating images from target_files zip.
  When `repacking` is True, redirect these properties to the actual files in the
  unzipped directory.

  Args:
    input_file: The input target_files file, which could be an open
        zipfile.ZipFile instance, or a str for the dir that contains the files
        unzipped from a target_files file.
    repacking: Whether it's trying repack an target_files file after loading the
        info dict (default: False). If so, it will rewrite a few loaded
        properties (e.g. selinux_fc, root_dir) to point to the actual files in
        target_files file. When doing repacking, `input_file` must be a dir.

  Returns:
    A dict that contains the parsed key/value pairs.

  Raises:
    AssertionError: On invalid input arguments.
    ValueError: On malformed input values.
  """
  if repacking:
    assert isinstance(input_file, str), \
        "input_file must be a path str when doing repacking"

  def read_helper(fn):
    if isinstance(input_file, zipfile.ZipFile):
      return input_file.read(fn).decode()
    else:
      path = os.path.join(input_file, *fn.split("/"))
      try:
        with open(path) as f:
          return f.read()
      except IOError as e:
        if e.errno == errno.ENOENT:
          raise KeyError(fn)

  try:
    d = LoadDictionaryFromLines(read_helper("META/misc_info.txt").split("\n"))
  except KeyError:
    raise ValueError("Failed to find META/misc_info.txt in input target-files")

  if "recovery_api_version" not in d:
    raise ValueError("Failed to find 'recovery_api_version'")
  if "fstab_version" not in d:
    raise ValueError("Failed to find 'fstab_version'")

  if repacking:
    # We carry a copy of file_contexts.bin under META/. If not available, search
    # BOOT/RAMDISK/. Note that sometimes we may need a different file to build
    # images than the one running on device, in that case, we must have the one
    # for image generation copied to META/.
    fc_basename = os.path.basename(d.get("selinux_fc", "file_contexts"))
    fc_config = os.path.join(input_file, "META", fc_basename)
    assert os.path.exists(fc_config)

    d["selinux_fc"] = fc_config

    # Similarly we need to redirect "root_dir", and "root_fs_config".
    d["root_dir"] = os.path.join(input_file, "ROOT")
    d["root_fs_config"] = os.path.join(
        input_file, "META", "root_filesystem_config.txt")

    # Redirect {system,vendor}_base_fs_file.
    if "system_base_fs_file" in d:
      basename = os.path.basename(d["system_base_fs_file"])
      system_base_fs_file = os.path.join(input_file, "META", basename)
      if os.path.exists(system_base_fs_file):
        d["system_base_fs_file"] = system_base_fs_file
      else:
        logger.warning(
            "Failed to find system base fs file: %s", system_base_fs_file)
        del d["system_base_fs_file"]

    if "vendor_base_fs_file" in d:
      basename = os.path.basename(d["vendor_base_fs_file"])
      vendor_base_fs_file = os.path.join(input_file, "META", basename)
      if os.path.exists(vendor_base_fs_file):
        d["vendor_base_fs_file"] = vendor_base_fs_file
      else:
        logger.warning(
            "Failed to find vendor base fs file: %s", vendor_base_fs_file)
        del d["vendor_base_fs_file"]

  def makeint(key):
    if key in d:
      d[key] = int(d[key], 0)

  makeint("recovery_api_version")
  makeint("blocksize")
  makeint("system_size")
  makeint("vendor_size")
  makeint("userdata_size")
  makeint("cache_size")
  makeint("recovery_size")
  makeint("boot_size")
  makeint("fstab_version")

  has_vendor = "vendor_fs_type" in d

  # We changed recovery.fstab path in Q, from ../RAMDISK/etc/recovery.fstab to
  # ../RAMDISK/system/etc/recovery.fstab. LoadInfoDict() has to handle both
  # cases, since it may load the info_dict from an old build (e.g. when
  # generating incremental OTAs from that build).
  system_root_image = d.get("system_root_image") == "true"
  if d.get("no_recovery") != "true":
    recovery_fstab_path = "RECOVERY/RAMDISK/system/etc/recovery.fstab"
    if isinstance(input_file, zipfile.ZipFile):
      if recovery_fstab_path not in input_file.namelist():
        recovery_fstab_path = "RECOVERY/RAMDISK/etc/recovery.fstab"
    else:
      path = os.path.join(input_file, *recovery_fstab_path.split("/"))
      if not os.path.exists(path):
        recovery_fstab_path = "RECOVERY/RAMDISK/etc/recovery.fstab"
    d["fstab"] = LoadRecoveryFSTab(
        read_helper, d["fstab_version"], recovery_fstab_path, system_root_image, has_vendor)

  elif d.get("recovery_as_boot") == "true":
    recovery_fstab_path = "BOOT/RAMDISK/system/etc/recovery.fstab"
    if isinstance(input_file, zipfile.ZipFile):
      if recovery_fstab_path not in input_file.namelist():
        recovery_fstab_path = "BOOT/RAMDISK/etc/recovery.fstab"
    else:
      path = os.path.join(input_file, *recovery_fstab_path.split("/"))
      if not os.path.exists(path):
        recovery_fstab_path = "BOOT/RAMDISK/etc/recovery.fstab"
    d["fstab"] = LoadRecoveryFSTab(
        read_helper, d["fstab_version"], recovery_fstab_path, system_root_image, has_vendor)

  else:
    d["fstab"] = None

  # Tries to load the build props for all partitions with care_map, including
  # system and vendor.
  for partition in PARTITIONS_WITH_CARE_MAP:
    partition_prop = "{}.build.prop".format(partition)
    d[partition_prop] = LoadBuildProp(
        read_helper, "{}/build.prop".format(partition.upper()))
    # Some partition might use /<partition>/etc/build.prop as the new path.
    # TODO: try new path first when majority of them switch to the new path.
    if not d[partition_prop]:
      d[partition_prop] = LoadBuildProp(
          read_helper, "{}/etc/build.prop".format(partition.upper()))
  d["build.prop"] = d["system.build.prop"]

  # Set up the salt (based on fingerprint or thumbprint) that will be used when
  # adding AVB footer.
  if d.get("avb_enable") == "true":
    fp = None
    if "build.prop" in d:
      build_prop = d["build.prop"]
      if "ro.build.fingerprint" in build_prop:
        fp = build_prop["ro.build.fingerprint"]
      elif "ro.build.thumbprint" in build_prop:
        fp = build_prop["ro.build.thumbprint"]
    if fp:
      d["avb_salt"] = sha256(fp.encode()).hexdigest()

  return d


def LoadBuildProp(read_helper, prop_file):
  try:
    data = read_helper(prop_file)
  except KeyError:
    logger.warning("Failed to read %s", prop_file)
    data = ""
  return LoadDictionaryFromLines(data.split("\n"))


def LoadDictionaryFromLines(lines):
  d = {}
  for line in lines:
    line = line.strip()
    if not line or line.startswith("#"):
      continue
    if "=" in line:
      name, value = line.split("=", 1)
      d[name] = value
  return d


def LoadRecoveryFSTab(read_helper, fstab_version, recovery_fstab_path,
                      system_root_image=False, force_vendor=False):
  class Partition(object):
    def __init__(self, mount_point, fs_type, device, length, context):
      self.mount_point = mount_point
      self.fs_type = fs_type
      self.device = device
      self.length = length
      self.context = context

  try:
    data = read_helper(recovery_fstab_path)
  except KeyError:
    logger.warning("Failed to find %s", recovery_fstab_path)
    data = ""

  assert fstab_version == 2

  d = {}
  for line in data.split("\n"):
    line = line.strip()
    if not line or line.startswith("#"):
      continue

    # <src> <mnt_point> <type> <mnt_flags and options> <fs_mgr_flags>
    pieces = line.split()
    if len(pieces) != 5:
      raise ValueError("malformed recovery.fstab line: \"%s\"" % (line,))

    # Ignore entries that are managed by vold.
    options = pieces[4]
    if "voldmanaged=" in options:
      continue

    # It's a good line, parse it.
    length = 0
    options = options.split(",")
    for i in options:
      if i.startswith("length="):
        length = int(i[7:])
      else:
        # Ignore all unknown options in the unified fstab.
        continue

    mount_flags = pieces[3]
    # Honor the SELinux context if present.
    context = None
    for i in mount_flags.split(","):
      if i.startswith("context="):
        context = i

    mount_point = pieces[1]
    if not d.get(mount_point):
        d[mount_point] = Partition(mount_point=mount_point, fs_type=pieces[2],
                                   device=pieces[0], length=length, context=context)

  # / is used for the system mount point when the root directory is included in
  # system. Other areas assume system is always at "/system" so point /system
  # at /.
  if system_root_image:
    assert '/system' not in d and '/' in d
    d["/system"] = d["/"]

  if force_vendor and not d.has_key("/vendor"):
    system_part = d["/system"]
    vendor_part = Partition(mount_point = "/vendor",
                            fs_type = system_part.fs_type,
                            device = system_part.device.replace("system", "vendor"),
                            length = 0,
                            context = system_part.context)
    vendor_part.mount_point = "/vendor"
    vendor_part.device = system_part.device.replace("system", "vendor")
    d["/vendor"] = vendor_part

  return d


def DumpInfoDict(d):
  for k, v in sorted(d.items()):
    logger.info("%-25s = (%s) %s", k, type(v).__name__, v)


def AppendAVBSigningArgs(cmd, partition):
  """Append signing arguments for avbtool."""
  # e.g., "--key path/to/signing_key --algorithm SHA256_RSA4096"
  key_path = OPTIONS.info_dict.get("avb_" + partition + "_key_path")
  algorithm = OPTIONS.info_dict.get("avb_" + partition + "_algorithm")
  if key_path and algorithm:
    cmd.extend(["--key", key_path, "--algorithm", algorithm])
  avb_salt = OPTIONS.info_dict.get("avb_salt")
  # make_vbmeta_image doesn't like "--salt" (and it's not needed).
  if avb_salt and not partition.startswith("vbmeta"):
    cmd.extend(["--salt", avb_salt])


def GetAvbChainedPartitionArg(partition, info_dict, key=None):
  """Constructs and returns the arg to build or verify a chained partition.

  Args:
    partition: The partition name.
    info_dict: The info dict to look up the key info and rollback index
        location.
    key: The key to be used for building or verifying the partition. Defaults to
        the key listed in info_dict.

  Returns:
    A string of form "partition:rollback_index_location:key" that can be used to
    build or verify vbmeta image.
  """
  if key is None:
    key = info_dict["avb_" + partition + "_key_path"]
  pubkey_path = ExtractAvbPublicKey(key)
  rollback_index_location = info_dict[
      "avb_" + partition + "_rollback_index_location"]
  return "{}:{}:{}".format(partition, rollback_index_location, pubkey_path)


def _BuildBootableImage(sourcedir, fs_config_file, info_dict=None,
                        has_ramdisk=False, two_step_image=False):
  """Build a bootable image from the specified sourcedir.

  Take a kernel, cmdline, and optionally a ramdisk directory from the input (in
  'sourcedir'), and turn them into a boot image. 'two_step_image' indicates if
  we are building a two-step special image (i.e. building a recovery image to
  be loaded into /boot in two-step OTAs).

  Return the image data, or None if sourcedir does not appear to contains files
  for building the requested image.
  """

  def make_ramdisk():
    ramdisk_img = tempfile.NamedTemporaryFile()

    if os.access(fs_config_file, os.F_OK):
      cmd = ["mkbootfs", "-f", fs_config_file,
             os.path.join(sourcedir, "RAMDISK")]
    else:
      cmd = ["mkbootfs", os.path.join(sourcedir, "RAMDISK")]
    p1 = Run(cmd, stdout=subprocess.PIPE)
    p2 = Run(["minigzip"], stdin=p1.stdout, stdout=ramdisk_img.file.fileno())

    p2.wait()
    p1.wait()
    assert p1.returncode == 0, "mkbootfs of %s ramdisk failed" % (sourcedir,)
    assert p2.returncode == 0, "minigzip of %s ramdisk failed" % (sourcedir,)

    return ramdisk_img

  if not os.access(os.path.join(sourcedir, "kernel"), os.F_OK):
    return None

  if has_ramdisk and not os.access(os.path.join(sourcedir, "RAMDISK"), os.F_OK):
    return None

  if info_dict is None:
    info_dict = OPTIONS.info_dict

  img = tempfile.NamedTemporaryFile()

  if has_ramdisk:
    ramdisk_img = make_ramdisk()

  # use MKBOOTIMG from environ, or "mkbootimg" if empty or not set
  mkbootimg = os.getenv('MKBOOTIMG') or "mkbootimg"

  cmd = [mkbootimg, "--kernel", os.path.join(sourcedir, "kernel")]

  fn = os.path.join(sourcedir, "second")
  if os.access(fn, os.F_OK):
    cmd.append("--second")
    cmd.append(fn)

  fn = os.path.join(sourcedir, "dtb")
  if os.access(fn, os.F_OK):
    cmd.append("--dtb")
    cmd.append(fn)

  fn = os.path.join(sourcedir, "cmdline")
  if os.access(fn, os.F_OK):
    cmd.append("--cmdline")
    cmd.append(open(fn).read().rstrip("\n"))

  fn = os.path.join(sourcedir, "base")
  if os.access(fn, os.F_OK):
    cmd.append("--base")
    cmd.append(open(fn).read().rstrip("\n"))

  fn = os.path.join(sourcedir, "pagesize")
  if os.access(fn, os.F_OK):
    cmd.append("--pagesize")
    cmd.append(open(fn).read().rstrip("\n"))

  fn = os.path.join(sourcedir, "tagsaddr")
  if os.access(fn, os.F_OK):
    cmd.append("--tags-addr")
    cmd.append(open(fn).read().rstrip("\n"))

  fn = os.path.join(sourcedir, "ramdisk_offset")
  if os.access(fn, os.F_OK):
    cmd.append("--ramdisk_offset")
    cmd.append(open(fn).read().rstrip("\n"))

  fn = os.path.join(sourcedir, "dt")
  if os.access(fn, os.F_OK):
    cmd.append("--dt")
    cmd.append(fn)

  args = info_dict.get("mkbootimg_args")
  if args and args.strip():
    cmd.extend(shlex.split(args))

  args = info_dict.get("mkbootimg_version_args")
  if args and args.strip():
    cmd.extend(shlex.split(args))

  if has_ramdisk:
    cmd.extend(["--ramdisk", ramdisk_img.name])

  img_unsigned = None
  if info_dict.get("vboot"):
    img_unsigned = tempfile.NamedTemporaryFile()
    cmd.extend(["--output", img_unsigned.name])
  else:
    cmd.extend(["--output", img.name])

  # "boot" or "recovery", without extension.
  partition_name = os.path.basename(sourcedir).lower()

  if partition_name == "recovery":
    if info_dict.get("include_recovery_dtbo") == "true":
      fn = os.path.join(sourcedir, "recovery_dtbo")
      cmd.extend(["--recovery_dtbo", fn])
    if info_dict.get("include_recovery_acpio") == "true":
      fn = os.path.join(sourcedir, "recovery_acpio")
      cmd.extend(["--recovery_acpio", fn])

  RunAndCheckOutput(cmd)

  if (info_dict.get("boot_signer") == "true" and
      info_dict.get("verity_key")):
    # Hard-code the path as "/boot" for two-step special recovery image (which
    # will be loaded into /boot during the two-step OTA).
    if two_step_image:
      path = "/boot"
    else:
      path = "/" + partition_name
    cmd = [OPTIONS.boot_signer_path]
    cmd.extend(OPTIONS.boot_signer_args)
    cmd.extend([path, img.name,
                info_dict["verity_key"] + ".pk8",
                info_dict["verity_key"] + ".x509.pem", img.name])
    RunAndCheckOutput(cmd)

  # Sign the image if vboot is non-empty.
  elif info_dict.get("vboot"):
    path = "/" + partition_name
    img_keyblock = tempfile.NamedTemporaryFile()
    # We have switched from the prebuilt futility binary to using the tool
    # (futility-host) built from the source. Override the setting in the old
    # TF.zip.
    futility = info_dict["futility"]
    if futility.startswith("prebuilts/"):
      futility = "futility-host"
    cmd = [info_dict["vboot_signer_cmd"], futility,
           img_unsigned.name, info_dict["vboot_key"] + ".vbpubk",
           info_dict["vboot_key"] + ".vbprivk",
           info_dict["vboot_subkey"] + ".vbprivk",
           img_keyblock.name,
           img.name]
    RunAndCheckOutput(cmd)

    # Clean up the temp files.
    img_unsigned.close()
    img_keyblock.close()

  # AVB: if enabled, calculate and add hash to boot.img or recovery.img.
  if info_dict.get("avb_enable") == "true":
    avbtool = info_dict["avb_avbtool"]
    part_size = info_dict[partition_name + "_size"]
    cmd = [avbtool, "add_hash_footer", "--image", img.name,
           "--partition_size", str(part_size), "--partition_name",
           partition_name]
    AppendAVBSigningArgs(cmd, partition_name)
    args = info_dict.get("avb_" + partition_name + "_add_hash_footer_args")
    if args and args.strip():
      cmd.extend(shlex.split(args))
    RunAndCheckOutput(cmd)

  img.seek(os.SEEK_SET, 0)
  data = img.read()

  if has_ramdisk:
    ramdisk_img.close()
  img.close()

  return data


def GetBootableImage(name, prebuilt_name, unpack_dir, tree_subdir,
                     info_dict=None, two_step_image=False):
  """Return a File object with the desired bootable image.

  Look for it in 'unpack_dir'/BOOTABLE_IMAGES under the name 'prebuilt_name',
  otherwise look for it under 'unpack_dir'/IMAGES, otherwise construct it from
  the source files in 'unpack_dir'/'tree_subdir'."""

  prebuilt_path = os.path.join(unpack_dir, "BOOTABLE_IMAGES", prebuilt_name)
  if os.path.exists(prebuilt_path):
    logger.info("using prebuilt %s from BOOTABLE_IMAGES...", prebuilt_name)
    return File.FromLocalFile(name, prebuilt_path)

  prebuilt_path = os.path.join(unpack_dir, "IMAGES", prebuilt_name)
  if os.path.exists(prebuilt_path):
    logger.info("using prebuilt %s from IMAGES...", prebuilt_name)
    return File.FromLocalFile(name, prebuilt_path)

  logger.info("building image from target_files %s...", tree_subdir)

  if info_dict is None:
    info_dict = OPTIONS.info_dict

  # With system_root_image == "true", we don't pack ramdisk into the boot image.
  # Unless "recovery_as_boot" is specified, in which case we carry the ramdisk
  # for recovery.
  has_ramdisk = (info_dict.get("system_root_image") != "true" or
                 prebuilt_name != "boot.img" or
                 info_dict.get("recovery_as_boot") == "true")

  fs_config = "META/" + tree_subdir.lower() + "_filesystem_config.txt"
  data = _BuildBootableImage(os.path.join(unpack_dir, tree_subdir),
                             os.path.join(unpack_dir, fs_config),
                             info_dict, has_ramdisk, two_step_image)
  if data:
    return File(name, data)
  return None


def Gunzip(in_filename, out_filename):
  """Gunzips the given gzip compressed file to a given output file."""
  with gzip.open(in_filename, "rb") as in_file, \
       open(out_filename, "wb") as out_file:
    shutil.copyfileobj(in_file, out_file)


def UnzipToDir(filename, dirname, patterns=None):
  """Unzips the archive to the given directory.

  Args:
    filename: The name of the zip file to unzip.
    dirname: Where the unziped files will land.
    patterns: Files to unzip from the archive. If omitted, will unzip the entire
        archvie. Non-matching patterns will be filtered out. If there's no match
        after the filtering, no file will be unzipped.
  """
  cmd = ["unzip", "-o", "-q", filename, "-d", dirname]
  if patterns is not None:
    # Filter out non-matching patterns. unzip will complain otherwise.
    with zipfile.ZipFile(filename) as input_zip:
      names = input_zip.namelist()
    filtered = [
        pattern for pattern in patterns if fnmatch.filter(names, pattern)]

    # There isn't any matching files. Don't unzip anything.
    if not filtered:
      return
    cmd.extend(filtered)

  RunAndCheckOutput(cmd)


def UnzipTemp(filename, pattern=None):
  """Unzips the given archive into a temporary directory and returns the name.

  Args:
    filename: If filename is of the form "foo.zip+bar.zip", unzip foo.zip into
    a temp dir, then unzip bar.zip into that_dir/BOOTABLE_IMAGES.

    pattern: Files to unzip from the archive. If omitted, will unzip the entire
    archvie.

  Returns:
    The name of the temporary directory.
  """

  tmp = MakeTempDir(prefix="targetfiles-")
  m = re.match(r"^(.*[.]zip)\+(.*[.]zip)$", filename, re.IGNORECASE)
  if m:
    UnzipToDir(m.group(1), tmp, pattern)
    UnzipToDir(m.group(2), os.path.join(tmp, "BOOTABLE_IMAGES"), pattern)
    filename = m.group(1)
  else:
    UnzipToDir(filename, tmp, pattern)

  return tmp


def GetUserImage(which, tmpdir, input_zip,
                 info_dict=None,
                 allow_shared_blocks=None,
                 hashtree_info_generator=None,
                 reset_file_map=False):
  """Returns an Image object suitable for passing to BlockImageDiff.

  This function loads the specified image from the given path. If the specified
  image is sparse, it also performs additional processing for OTA purpose. For
  example, it always adds block 0 to clobbered blocks list. It also detects
  files that cannot be reconstructed from the block list, for whom we should
  avoid applying imgdiff.

  Args:
    which: The partition name.
    tmpdir: The directory that contains the prebuilt image and block map file.
    input_zip: The target-files ZIP archive.
    info_dict: The dict to be looked up for relevant info.
    allow_shared_blocks: If image is sparse, whether having shared blocks is
        allowed. If none, it is looked up from info_dict.
    hashtree_info_generator: If present and image is sparse, generates the
        hashtree_info for this sparse image.
    reset_file_map: If true and image is sparse, reset file map before returning
        the image.
  Returns:
    A Image object. If it is a sparse image and reset_file_map is False, the
    image will have file_map info loaded.
  """
  if info_dict is None:
    info_dict = LoadInfoDict(input_zip)

  is_sparse = info_dict.get("extfs_sparse_flag")

  # When target uses 'BOARD_EXT4_SHARE_DUP_BLOCKS := true', images may contain
  # shared blocks (i.e. some blocks will show up in multiple files' block
  # list). We can only allocate such shared blocks to the first "owner", and
  # disable imgdiff for all later occurrences.
  if allow_shared_blocks is None:
    allow_shared_blocks = info_dict.get("ext4_share_dup_blocks") == "true"

  if is_sparse:
    img = GetSparseImage(which, tmpdir, input_zip, allow_shared_blocks,
                         hashtree_info_generator)
    if reset_file_map:
      img.ResetFileMap()
    return img
  else:
    return GetNonSparseImage(which, tmpdir, hashtree_info_generator)


def GetNonSparseImage(which, tmpdir, hashtree_info_generator=None):
  """Returns a Image object suitable for passing to BlockImageDiff.

  This function loads the specified non-sparse image from the given path.

  Args:
    which: The partition name.
    tmpdir: The directory that contains the prebuilt image and block map file.
  Returns:
    A Image object.
  """
  path = os.path.join(tmpdir, "IMAGES", which + ".img")
  mappath = os.path.join(tmpdir, "IMAGES", which + ".map")

  # The image and map files must have been created prior to calling
  # ota_from_target_files.py (since LMP).
  assert os.path.exists(path) and os.path.exists(mappath)

  return blockimgdiff.FileImage(path, hashtree_info_generator=
                                hashtree_info_generator)

def GetSparseImage(which, tmpdir, input_zip, allow_shared_blocks,
                   hashtree_info_generator=None):
  """Returns a SparseImage object suitable for passing to BlockImageDiff.

  This function loads the specified sparse image from the given path, and
  performs additional processing for OTA purpose. For example, it always adds
  block 0 to clobbered blocks list. It also detects files that cannot be
  reconstructed from the block list, for whom we should avoid applying imgdiff.

  Args:
    which: The partition name, e.g. "system", "vendor".
    tmpdir: The directory that contains the prebuilt image and block map file.
    input_zip: The target-files ZIP archive.
    allow_shared_blocks: Whether having shared blocks is allowed.
    hashtree_info_generator: If present, generates the hashtree_info for this
        sparse image.
  Returns:
    A SparseImage object, with file_map info loaded.
  """
  path = os.path.join(tmpdir, "IMAGES", which + ".img")
  mappath = os.path.join(tmpdir, "IMAGES", which + ".map")

  # The image and map files must have been created prior to calling
  # ota_from_target_files.py (since LMP).
  assert os.path.exists(path) and os.path.exists(mappath)

  # In ext4 filesystems, block 0 might be changed even being mounted R/O. We add
  # it to clobbered_blocks so that it will be written to the target
  # unconditionally. Note that they are still part of care_map. (Bug: 20939131)
  clobbered_blocks = "0"

  image = sparse_img.SparseImage(
      path, mappath, clobbered_blocks, allow_shared_blocks=allow_shared_blocks,
      hashtree_info_generator=hashtree_info_generator)

  # block.map may contain less blocks, because mke2fs may skip allocating blocks
  # if they contain all zeros. We can't reconstruct such a file from its block
  # list. Tag such entries accordingly. (Bug: 65213616)
  for entry in image.file_map:
    # Skip artificial names, such as "__ZERO", "__NONZERO-1".
    if not entry.startswith('/'):
      continue

    # "/system/framework/am.jar" => "SYSTEM/framework/am.jar". Note that the
    # filename listed in system.map may contain an additional leading slash
    # (i.e. "//system/framework/am.jar"). Using lstrip to get consistent
    # results.
    arcname = entry.replace(which, which.upper(), 1).lstrip('/')

    # Special handling another case, where files not under /system
    # (e.g. "/sbin/charger") are packed under ROOT/ in a target_files.zip.
    if which == 'system' and not arcname.startswith('SYSTEM'):
      arcname = 'ROOT/' + arcname

    assert arcname in input_zip.namelist(), \
        "Failed to find the ZIP entry for {}".format(entry)

    info = input_zip.getinfo(arcname)
    ranges = image.file_map[entry]

    # If a RangeSet has been tagged as using shared blocks while loading the
    # image, check the original block list to determine its completeness. Note
    # that the 'incomplete' flag would be tagged to the original RangeSet only.
    if ranges.extra.get('uses_shared_blocks'):
      ranges = ranges.extra['uses_shared_blocks']

    if RoundUpTo4K(info.file_size) > ranges.size() * 4096:
      ranges.extra['incomplete'] = True

  return image


def GetKeyPasswords(keylist):
  """Given a list of keys, prompt the user to enter passwords for
  those which require them.  Return a {key: password} dict.  password
  will be None if the key has no password."""

  no_passwords = []
  need_passwords = []
  key_passwords = {}
  devnull = open("/dev/null", "w+b")
  for k in sorted(keylist):
    # We don't need a password for things that aren't really keys.
    if k in SPECIAL_CERT_STRINGS:
      no_passwords.append(k)
      continue

    p = Run(["openssl", "pkcs8", "-in", k+OPTIONS.private_key_suffix,
             "-inform", "DER", "-nocrypt"],
            stdin=devnull.fileno(),
            stdout=devnull.fileno(),
            stderr=subprocess.STDOUT)
    p.communicate()
    if p.returncode == 0:
      # Definitely an unencrypted key.
      no_passwords.append(k)
    else:
      p = Run(["openssl", "pkcs8", "-in", k+OPTIONS.private_key_suffix,
               "-inform", "DER", "-passin", "pass:"],
              stdin=devnull.fileno(),
              stdout=devnull.fileno(),
              stderr=subprocess.PIPE)
      _, stderr = p.communicate()
      if p.returncode == 0:
        # Encrypted key with empty string as password.
        key_passwords[k] = ''
      elif stderr.startswith('Error decrypting key'):
        # Definitely encrypted key.
        # It would have said "Error reading key" if it didn't parse correctly.
        need_passwords.append(k)
      else:
        # Potentially, a type of key that openssl doesn't understand.
        # We'll let the routines in signapk.jar handle it.
        no_passwords.append(k)
  devnull.close()

  key_passwords.update(PasswordManager().GetPasswords(need_passwords))
  key_passwords.update(dict.fromkeys(no_passwords))
  return key_passwords


def GetMinSdkVersion(apk_name):
  """Gets the minSdkVersion declared in the APK.

  It calls 'aapt2' to query the embedded minSdkVersion from the given APK file.
  This can be both a decimal number (API Level) or a codename.

  Args:
    apk_name: The APK filename.

  Returns:
    The parsed SDK version string.

  Raises:
    ExternalError: On failing to obtain the min SDK version.
  """
  proc = Run(
      ["aapt2", "dump", "badging", apk_name], stdout=subprocess.PIPE,
      stderr=subprocess.PIPE)
  stdoutdata, stderrdata = proc.communicate()
  if proc.returncode != 0:
    raise ExternalError(
        "Failed to obtain minSdkVersion: aapt2 return code {}:\n{}\n{}".format(
            proc.returncode, stdoutdata, stderrdata))

  for line in stdoutdata.split("\n"):
    # Looking for lines such as sdkVersion:'23' or sdkVersion:'M'.
    m = re.match(r'sdkVersion:\'([^\']*)\'', line)
    if m:
      return m.group(1)
  raise ExternalError("No minSdkVersion returned by aapt2")


def GetMinSdkVersionInt(apk_name, codename_to_api_level_map):
  """Returns the minSdkVersion declared in the APK as a number (API Level).

  If minSdkVersion is set to a codename, it is translated to a number using the
  provided map.

  Args:
    apk_name: The APK filename.

  Returns:
    The parsed SDK version number.

  Raises:
    ExternalError: On failing to get the min SDK version number.
  """
  version = GetMinSdkVersion(apk_name)
  try:
    return int(version)
  except ValueError:
    # Not a decimal number. Codename?
    if version in codename_to_api_level_map:
      return codename_to_api_level_map[version]
    else:
      raise ExternalError(
          "Unknown minSdkVersion: '{}'. Known codenames: {}".format(
              version, codename_to_api_level_map))


def SignFile(input_name, output_name, key, password, min_api_level=None,
             codename_to_api_level_map=None, whole_file=False,
             extra_signapk_args=None):
  """Sign the input_name zip/jar/apk, producing output_name.  Use the
  given key and password (the latter may be None if the key does not
  have a password.

  If whole_file is true, use the "-w" option to SignApk to embed a
  signature that covers the whole file in the archive comment of the
  zip file.

  min_api_level is the API Level (int) of the oldest platform this file may end
  up on. If not specified for an APK, the API Level is obtained by interpreting
  the minSdkVersion attribute of the APK's AndroidManifest.xml.

  codename_to_api_level_map is needed to translate the codename which may be
  encountered as the APK's minSdkVersion.

  Caller may optionally specify extra args to be passed to SignApk, which
  defaults to OPTIONS.extra_signapk_args if omitted.
  """
  if codename_to_api_level_map is None:
    codename_to_api_level_map = {}
  if extra_signapk_args is None:
    extra_signapk_args = OPTIONS.extra_signapk_args

  java_library_path = os.path.join(
      OPTIONS.search_path, OPTIONS.signapk_shared_library_path)

  cmd = ([OPTIONS.java_path] + OPTIONS.java_args +
         ["-Djava.library.path=" + java_library_path,
          "-jar", os.path.join(OPTIONS.search_path, OPTIONS.signapk_path)] +
         extra_signapk_args)
  if whole_file:
    cmd.append("-w")

  min_sdk_version = min_api_level
  if min_sdk_version is None:
    if not whole_file:
      min_sdk_version = GetMinSdkVersionInt(
          input_name, codename_to_api_level_map)
  if min_sdk_version is not None:
    cmd.extend(["--min-sdk-version", str(min_sdk_version)])

  cmd.extend([key + OPTIONS.public_key_suffix,
              key + OPTIONS.private_key_suffix,
              input_name, output_name])

  proc = Run(cmd, stdin=subprocess.PIPE)
  if password is not None:
    password += "\n"
  stdoutdata, _ = proc.communicate(password)
  if proc.returncode != 0:
    raise ExternalError(
        "Failed to run signapk.jar: return code {}:\n{}".format(
            proc.returncode, stdoutdata))


def CheckSize(data, target, info_dict):
  """Checks the data string passed against the max size limit.

  For non-AVB images, raise exception if the data is too big. Print a warning
  if the data is nearing the maximum size.

  For AVB images, the actual image size should be identical to the limit.

  Args:
    data: A string that contains all the data for the partition.
    target: The partition name. The ".img" suffix is optional.
    info_dict: The dict to be looked up for relevant info.
  """
  if target.endswith(".img"):
    target = target[:-4]
  mount_point = "/" + target

  fs_type = None
  limit = None
  if info_dict["fstab"]:
    if mount_point == "/userdata":
      mount_point = "/data"
    p = info_dict["fstab"][mount_point]
    fs_type = p.fs_type
    device = p.device
    if "/" in device:
      device = device[device.rfind("/")+1:]
    limit = info_dict.get(device + "_size")
  if not fs_type or not limit:
    return

  size = len(data)
  # target could be 'userdata' or 'cache'. They should follow the non-AVB image
  # path.
  if info_dict.get("avb_enable") == "true" and target in AVB_PARTITIONS:
    if size != limit:
      raise ExternalError(
          "Mismatching image size for %s: expected %d actual %d" % (
              target, limit, size))
  else:
    pct = float(size) * 100.0 / limit
    msg = "%s size (%d) is %.2f%% of limit (%d)" % (target, size, pct, limit)
    if pct >= 99.0:
      raise ExternalError(msg)
    elif pct >= 95.0:
      logger.warning("\n  WARNING: %s\n", msg)
    else:
      logger.info("  %s", msg)


def ReadApkCerts(tf_zip):
  """Parses the APK certs info from a given target-files zip.

  Given a target-files ZipFile, parses the META/apkcerts.txt entry and returns a
  tuple with the following elements: (1) a dictionary that maps packages to
  certs (based on the "certificate" and "private_key" attributes in the file;
  (2) a string representing the extension of compressed APKs in the target files
  (e.g ".gz", ".bro").

  Args:
    tf_zip: The input target_files ZipFile (already open).

  Returns:
    (certmap, ext): certmap is a dictionary that maps packages to certs; ext is
        the extension string of compressed APKs (e.g. ".gz"), or None if there's
        no compressed APKs.
  """
  certmap = {}
  compressed_extension = None

  # META/apkcerts.txt contains the info for _all_ the packages known at build
  # time. Filter out the ones that are not installed.
  installed_files = set()
  for name in tf_zip.namelist():
    basename = os.path.basename(name)
    if basename:
      installed_files.add(basename)

  for line in tf_zip.read('META/apkcerts.txt').decode().split('\n'):
    line = line.strip()
    if not line:
      continue
    m = re.match(
        r'^name="(?P<NAME>.*)"\s+certificate="(?P<CERT>.*)"\s+'
        r'private_key="(?P<PRIVKEY>.*?)"(\s+compressed="(?P<COMPRESSED>.*)")?$',
        line)
    if not m:
      continue

    matches = m.groupdict()
    cert = matches["CERT"]
    privkey = matches["PRIVKEY"]
    name = matches["NAME"]
    this_compressed_extension = matches["COMPRESSED"]

    public_key_suffix_len = len(OPTIONS.public_key_suffix)
    private_key_suffix_len = len(OPTIONS.private_key_suffix)
    if cert in SPECIAL_CERT_STRINGS and not privkey:
      certmap[name] = cert
    elif (cert.endswith(OPTIONS.public_key_suffix) and
          privkey.endswith(OPTIONS.private_key_suffix) and
          cert[:-public_key_suffix_len] == privkey[:-private_key_suffix_len]):
      certmap[name] = cert[:-public_key_suffix_len]
    else:
      raise ValueError("Failed to parse line from apkcerts.txt:\n" + line)

    if not this_compressed_extension:
      continue

    # Only count the installed files.
    filename = name + '.' + this_compressed_extension
    if filename not in installed_files:
      continue

    # Make sure that all the values in the compression map have the same
    # extension. We don't support multiple compression methods in the same
    # system image.
    if compressed_extension:
      if this_compressed_extension != compressed_extension:
        raise ValueError(
            "Multiple compressed extensions: {} vs {}".format(
                compressed_extension, this_compressed_extension))
    else:
      compressed_extension = this_compressed_extension

  return (certmap,
          ("." + compressed_extension) if compressed_extension else None)


COMMON_DOCSTRING = """
Global options

  -p  (--path) <dir>
      Prepend <dir>/bin to the list of places to search for binaries run by this
      script, and expect to find jars in <dir>/framework.

  -s  (--device_specific) <file>
      Path to the Python module containing device-specific releasetools code.

  -x  (--extra) <key=value>
      Add a key/value pair to the 'extras' dict, which device-specific extension
      code may look at.

  -v  (--verbose)
      Show command lines being executed.

  -h  (--help)
      Display this usage message and exit.
"""

def Usage(docstring):
  print(docstring.rstrip("\n"))
  print(COMMON_DOCSTRING)


def ParseOptions(argv,
                 docstring,
                 extra_opts="", extra_long_opts=(),
                 extra_option_handler=None):
  """Parse the options in argv and return any arguments that aren't
  flags.  docstring is the calling module's docstring, to be displayed
  for errors and -h.  extra_opts and extra_long_opts are for flags
  defined by the caller, which are processed by passing them to
  extra_option_handler."""

  try:
    opts, args = getopt.getopt(
        argv, "hvp:s:x:" + extra_opts,
        ["help", "verbose", "path=", "signapk_path=",
         "signapk_shared_library_path=", "extra_signapk_args=",
         "java_path=", "java_args=", "public_key_suffix=",
         "private_key_suffix=", "boot_signer_path=", "boot_signer_args=",
         "verity_signer_path=", "verity_signer_args=", "device_specific=",
         "extra="] +
        list(extra_long_opts))
  except getopt.GetoptError as err:
    Usage(docstring)
    print("**", str(err), "**")
    sys.exit(2)

  for o, a in opts:
    if o in ("-h", "--help"):
      Usage(docstring)
      sys.exit()
    elif o in ("-v", "--verbose"):
      OPTIONS.verbose = True
    elif o in ("-p", "--path"):
      OPTIONS.search_path = a
    elif o in ("--signapk_path",):
      OPTIONS.signapk_path = a
    elif o in ("--signapk_shared_library_path",):
      OPTIONS.signapk_shared_library_path = a
    elif o in ("--extra_signapk_args",):
      OPTIONS.extra_signapk_args = shlex.split(a)
    elif o in ("--java_path",):
      OPTIONS.java_path = a
    elif o in ("--java_args",):
      OPTIONS.java_args = shlex.split(a)
    elif o in ("--public_key_suffix",):
      OPTIONS.public_key_suffix = a
    elif o in ("--private_key_suffix",):
      OPTIONS.private_key_suffix = a
    elif o in ("--boot_signer_path",):
      OPTIONS.boot_signer_path = a
    elif o in ("--boot_signer_args",):
      OPTIONS.boot_signer_args = shlex.split(a)
    elif o in ("--verity_signer_path",):
      OPTIONS.verity_signer_path = a
    elif o in ("--verity_signer_args",):
      OPTIONS.verity_signer_args = shlex.split(a)
    elif o in ("-s", "--device_specific"):
      OPTIONS.device_specific = a
    elif o in ("-x", "--extra"):
      key, value = a.split("=", 1)
      OPTIONS.extras[key] = value
    else:
      if extra_option_handler is None or not extra_option_handler(o, a):
        assert False, "unknown option \"%s\"" % (o,)

  if OPTIONS.search_path:
    os.environ["PATH"] = (os.path.join(OPTIONS.search_path, "bin") +
                          os.pathsep + os.environ["PATH"])

  return args


def MakeTempFile(prefix='tmp', suffix=''):
  """Make a temp file and add it to the list of things to be deleted
  when Cleanup() is called.  Return the filename."""
  fd, fn = tempfile.mkstemp(prefix=prefix, suffix=suffix)
  os.close(fd)
  OPTIONS.tempfiles.append(fn)
  return fn


def MakeTempDir(prefix='tmp', suffix=''):
  """Makes a temporary dir that will be cleaned up with a call to Cleanup().

  Returns:
    The absolute pathname of the new directory.
  """
  dir_name = tempfile.mkdtemp(suffix=suffix, prefix=prefix)
  OPTIONS.tempfiles.append(dir_name)
  return dir_name


def Cleanup():
  for i in OPTIONS.tempfiles:
    if os.path.isdir(i):
      shutil.rmtree(i, ignore_errors=True)
    else:
      os.remove(i)
  del OPTIONS.tempfiles[:]


class PasswordManager(object):
  def __init__(self):
    self.editor = os.getenv("EDITOR")
    self.pwfile = os.getenv("ANDROID_PW_FILE")
    self.secure_storage_cmd = os.getenv("ANDROID_SECURE_STORAGE_CMD", None)

  def GetPasswords(self, items):
    """Get passwords corresponding to each string in 'items',
    returning a dict.  (The dict may have keys in addition to the
    values in 'items'.)

    Uses the passwords in $ANDROID_PW_FILE if available, letting the
    user edit that file to add more needed passwords.  If no editor is
    available, or $ANDROID_PW_FILE isn't define, prompts the user
    interactively in the ordinary way.
    """

    current = self.ReadFile()

    first = True
    while True:
      missing = []
      for i in items:
        if i not in current or not current[i]:
          # Attempt to load using ANDROID_SECURE_STORAGE_CMD
          if self.secure_storage_cmd:
            try:
              os.environ["TMP__KEY_FILE_NAME"] = str(i)
              ps = subprocess.Popen(self.secure_storage_cmd, shell=True, stdout=subprocess.PIPE)
              output = ps.communicate()[0]
              if ps.returncode == 0:
                current[i] = output
            except Exception as e:
              print(e)
              pass
          if i not in current or not current[i]:
            missing.append(i)
      # Are all the passwords already in the file?
      if not missing:
        if "ANDROID_SECURE_STORAGE_CMD" in os.environ:
          del os.environ["ANDROID_SECURE_STORAGE_CMD"]
        return current

      for i in missing:
        current[i] = ""

      if not first:
        print("key file %s still missing some passwords." % (self.pwfile,))
        if sys.version_info[0] >= 3:
          raw_input = input  # pylint: disable=redefined-builtin
        answer = raw_input("try to edit again? [y]> ").strip()
        if answer and answer[0] not in 'yY':
          raise RuntimeError("key passwords unavailable")
      first = False

      current = self.UpdateAndReadFile(current)

  def PromptResult(self, current): # pylint: disable=no-self-use
    """Prompt the user to enter a value (password) for each key in
    'current' whose value is fales.  Returns a new dict with all the
    values.
    """
    result = {}
    for k, v in sorted(current.items()):
      if v:
        result[k] = v
      else:
        while True:
          result[k] = getpass.getpass(
              "Enter password for %s key> " % k).strip()
          if result[k]:
            break
    return result

  def UpdateAndReadFile(self, current):
    if not self.editor or not self.pwfile:
      return self.PromptResult(current)

    f = open(self.pwfile, "w")
    os.chmod(self.pwfile, 0o600)
    f.write("# Enter key passwords between the [[[ ]]] brackets.\n")
    f.write("# (Additional spaces are harmless.)\n\n")

    first_line = None
    sorted_list = sorted([(not v, k, v) for (k, v) in current.items()])
    for i, (_, k, v) in enumerate(sorted_list):
      f.write("[[[  %s  ]]] %s\n" % (v, k))
      if not v and first_line is None:
        # position cursor on first line with no password.
        first_line = i + 4
    f.close()

    RunAndCheckOutput([self.editor, "+%d" % (first_line,), self.pwfile])

    return self.ReadFile()

  def ReadFile(self):
    result = {}
    if self.pwfile is None:
      return result
    try:
      f = open(self.pwfile, "r")
      for line in f:
        line = line.strip()
        if not line or line[0] == '#':
          continue
        m = re.match(r"^\[\[\[\s*(.*?)\s*\]\]\]\s*(\S+)$", line)
        if not m:
          logger.warning("Failed to parse password file: %s", line)
        else:
          result[m.group(2)] = m.group(1)
      f.close()
    except IOError as e:
      if e.errno != errno.ENOENT:
        logger.exception("Error reading password file:")
    return result


def ZipWrite(zip_file, filename, arcname=None, perms=0o644,
             compress_type=None):
  import datetime

  # http://b/18015246
  # Python 2.7's zipfile implementation wrongly thinks that zip64 is required
  # for files larger than 2GiB. We can work around this by adjusting their
  # limit. Note that `zipfile.writestr()` will not work for strings larger than
  # 2GiB. The Python interpreter sometimes rejects strings that large (though
  # it isn't clear to me exactly what circumstances cause this).
  # `zipfile.write()` must be used directly to work around this.
  #
  # This mess can be avoided if we port to python3.
  saved_zip64_limit = zipfile.ZIP64_LIMIT
  zipfile.ZIP64_LIMIT = (1 << 32) - 1

  if compress_type is None:
    compress_type = zip_file.compression
  if arcname is None:
    arcname = filename

  saved_stat = os.stat(filename)

  try:
    # `zipfile.write()` doesn't allow us to pass ZipInfo, so just modify the
    # file to be zipped and reset it when we're done.
    os.chmod(filename, perms)

    # Use a fixed timestamp so the output is repeatable.
    # Note: Use of fromtimestamp rather than utcfromtimestamp here is
    # intentional. zip stores datetimes in local time without a time zone
    # attached, so we need "epoch" but in the local time zone to get 2009/01/01
    # in the zip archive.
    local_epoch = datetime.datetime.fromtimestamp(0)
    timestamp = (datetime.datetime(2009, 1, 1) - local_epoch).total_seconds()
    os.utime(filename, (timestamp, timestamp))

    zip_file.write(filename, arcname=arcname, compress_type=compress_type)
  finally:
    os.chmod(filename, saved_stat.st_mode)
    os.utime(filename, (saved_stat.st_atime, saved_stat.st_mtime))
    zipfile.ZIP64_LIMIT = saved_zip64_limit


def ZipWriteStr(zip_file, zinfo_or_arcname, data, perms=None,
                compress_type=None):
  """Wrap zipfile.writestr() function to work around the zip64 limit.

  Even with the ZIP64_LIMIT workaround, it won't allow writing a string
  longer than 2GiB. It gives 'OverflowError: size does not fit in an int'
  when calling crc32(bytes).

  But it still works fine to write a shorter string into a large zip file.
  We should use ZipWrite() whenever possible, and only use ZipWriteStr()
  when we know the string won't be too long.
  """

  saved_zip64_limit = zipfile.ZIP64_LIMIT
  zipfile.ZIP64_LIMIT = (1 << 32) - 1

  if not isinstance(zinfo_or_arcname, zipfile.ZipInfo):
    zinfo = zipfile.ZipInfo(filename=zinfo_or_arcname)
    zinfo.compress_type = zip_file.compression
    if perms is None:
      perms = 0o100644
  else:
    zinfo = zinfo_or_arcname
    # Python 2 and 3 behave differently when calling ZipFile.writestr() with
    # zinfo.external_attr being 0. Python 3 uses `0o600 << 16` as the value for
    # such a case (since
    # https://github.com/python/cpython/commit/18ee29d0b870caddc0806916ca2c823254f1a1f9),
    # which seems to make more sense. Otherwise the entry will have 0o000 as the
    # permission bits. We follow the logic in Python 3 to get consistent
    # behavior between using the two versions.
    if not zinfo.external_attr:
      zinfo.external_attr = 0o600 << 16

  # If compress_type is given, it overrides the value in zinfo.
  if compress_type is not None:
    zinfo.compress_type = compress_type

  # If perms is given, it has a priority.
  if perms is not None:
    # If perms doesn't set the file type, mark it as a regular file.
    if perms & 0o770000 == 0:
      perms |= 0o100000
    zinfo.external_attr = perms << 16

  # Use a fixed timestamp so the output is repeatable.
  zinfo.date_time = (2009, 1, 1, 0, 0, 0)

  zip_file.writestr(zinfo, data)
  zipfile.ZIP64_LIMIT = saved_zip64_limit


def ZipDelete(zip_filename, entries):
  """Deletes entries from a ZIP file.

  Since deleting entries from a ZIP file is not supported, it shells out to
  'zip -d'.

  Args:
    zip_filename: The name of the ZIP file.
    entries: The name of the entry, or the list of names to be deleted.

  Raises:
    AssertionError: In case of non-zero return from 'zip'.
  """
  if isinstance(entries, str):
    entries = [entries]
  cmd = ["zip", "-d", zip_filename] + entries
  RunAndCheckOutput(cmd)


def ZipClose(zip_file):
  # http://b/18015246
  # zipfile also refers to ZIP64_LIMIT during close() when it writes out the
  # central directory.
  saved_zip64_limit = zipfile.ZIP64_LIMIT
  zipfile.ZIP64_LIMIT = (1 << 32) - 1

  zip_file.close()

  zipfile.ZIP64_LIMIT = saved_zip64_limit


class DeviceSpecificParams(object):
  module = None
  def __init__(self, **kwargs):
    """Keyword arguments to the constructor become attributes of this
    object, which is passed to all functions in the device-specific
    module."""
    for k, v in kwargs.items():
      setattr(self, k, v)
    self.extras = OPTIONS.extras

    if self.module is None:
      path = OPTIONS.device_specific
      if not path:
        return
      try:
        if os.path.isdir(path):
          info = imp.find_module("releasetools", [path])
        else:
          d, f = os.path.split(path)
          b, x = os.path.splitext(f)
          if x == ".py":
            f = b
          info = imp.find_module(f, [d])
        logger.info("loaded device-specific extensions from %s", path)
        self.module = imp.load_module("device_specific", *info)
      except ImportError:
        logger.info("unable to load device-specific module; assuming none")

  def _DoCall(self, function_name, *args, **kwargs):
    """Call the named function in the device-specific module, passing
    the given args and kwargs.  The first argument to the call will be
    the DeviceSpecific object itself.  If there is no module, or the
    module does not define the function, return the value of the
    'default' kwarg (which itself defaults to None)."""
    if self.module is None or not hasattr(self.module, function_name):
      return kwargs.get("default")
    return getattr(self.module, function_name)(*((self,) + args), **kwargs)

  def FullOTA_Assertions(self):
    """Called after emitting the block of assertions at the top of a
    full OTA package.  Implementations can add whatever additional
    assertions they like."""
    return self._DoCall("FullOTA_Assertions")

  def FullOTA_InstallBegin(self):
    """Called at the start of full OTA installation."""
    return self._DoCall("FullOTA_InstallBegin")

  def FullOTA_GetBlockDifferences(self):
    """Called during full OTA installation and verification.
    Implementation should return a list of BlockDifference objects describing
    the update on each additional partitions.
    """
    return self._DoCall("FullOTA_GetBlockDifferences")

  def FullOTA_InstallEnd(self):
    """Called at the end of full OTA installation; typically this is
    used to install the image for the device's baseband processor."""
    return self._DoCall("FullOTA_InstallEnd")

  def FullOTA_PostValidate(self):
    """Called after installing and validating /system; typically this is
    used to resize the system partition after a block based installation."""
    return self._DoCall("FullOTA_PostValidate")

  def IncrementalOTA_Assertions(self):
    """Called after emitting the block of assertions at the top of an
    incremental OTA package.  Implementations can add whatever
    additional assertions they like."""
    return self._DoCall("IncrementalOTA_Assertions")

  def IncrementalOTA_VerifyBegin(self):
    """Called at the start of the verification phase of incremental
    OTA installation; additional checks can be placed here to abort
    the script before any changes are made."""
    return self._DoCall("IncrementalOTA_VerifyBegin")

  def IncrementalOTA_VerifyEnd(self):
    """Called at the end of the verification phase of incremental OTA
    installation; additional checks can be placed here to abort the
    script before any changes are made."""
    return self._DoCall("IncrementalOTA_VerifyEnd")

  def IncrementalOTA_InstallBegin(self):
    """Called at the start of incremental OTA installation (after
    verification is complete)."""
    return self._DoCall("IncrementalOTA_InstallBegin")

  def IncrementalOTA_GetBlockDifferences(self):
    """Called during incremental OTA installation and verification.
    Implementation should return a list of BlockDifference objects describing
    the update on each additional partitions.
    """
    return self._DoCall("IncrementalOTA_GetBlockDifferences")

  def IncrementalOTA_InstallEnd(self):
    """Called at the end of incremental OTA installation; typically
    this is used to install the image for the device's baseband
    processor."""
    return self._DoCall("IncrementalOTA_InstallEnd")

  def VerifyOTA_Assertions(self):
    return self._DoCall("VerifyOTA_Assertions")


class File(object):
  def __init__(self, name, data, compress_size=None):
    self.name = name
    self.data = data
    self.size = len(data)
    self.compress_size = compress_size or self.size
    self.sha1 = sha1(data).hexdigest()

  @classmethod
  def FromLocalFile(cls, name, diskname):
    f = open(diskname, "rb")
    data = f.read()
    f.close()
    return File(name, data)

  def WriteToTemp(self):
    t = tempfile.NamedTemporaryFile()
    t.write(self.data)
    t.flush()
    return t

  def WriteToDir(self, d):
    with open(os.path.join(d, self.name), "wb") as fp:
      fp.write(self.data)

  def AddToZip(self, z, compression=None):
    ZipWriteStr(z, self.name, self.data, compress_type=compression)


DIFF_PROGRAM_BY_EXT = {
    ".gz" : "imgdiff",
    ".zip" : ["imgdiff", "-z"],
    ".jar" : ["imgdiff", "-z"],
    ".apk" : ["imgdiff", "-z"],
    ".img" : "imgdiff",
    }


class Difference(object):
  def __init__(self, tf, sf, diff_program=None):
    self.tf = tf
    self.sf = sf
    self.patch = None
    self.diff_program = diff_program

  def ComputePatch(self):
    """Compute the patch (as a string of data) needed to turn sf into
    tf.  Returns the same tuple as GetPatch()."""

    tf = self.tf
    sf = self.sf

    if self.diff_program:
      diff_program = self.diff_program
    else:
      ext = os.path.splitext(tf.name)[1]
      diff_program = DIFF_PROGRAM_BY_EXT.get(ext, "bsdiff")

    ttemp = tf.WriteToTemp()
    stemp = sf.WriteToTemp()

    ext = os.path.splitext(tf.name)[1]

    try:
      ptemp = tempfile.NamedTemporaryFile()
      if isinstance(diff_program, list):
        cmd = copy.copy(diff_program)
      else:
        cmd = [diff_program]
      cmd.append(stemp.name)
      cmd.append(ttemp.name)
      cmd.append(ptemp.name)
      p = Run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      err = []
      def run():
        _, e = p.communicate()
        if e:
          err.append(e)
      th = threading.Thread(target=run)
      th.start()
      th.join(timeout=300)   # 5 mins
      if th.is_alive():
        logger.warning("diff command timed out")
        p.terminate()
        th.join(5)
        if th.is_alive():
          p.kill()
          th.join()

      if p.returncode != 0:
        logger.warning("Failure running %s:\n%s\n", diff_program, "".join(err))
        self.patch = None
        return None, None, None
      diff = ptemp.read()
    finally:
      ptemp.close()
      stemp.close()
      ttemp.close()

    self.patch = diff
    return self.tf, self.sf, self.patch


  def GetPatch(self):
    """Returns a tuple of (target_file, source_file, patch_data).

    patch_data may be None if ComputePatch hasn't been called, or if
    computing the patch failed.
    """
    return self.tf, self.sf, self.patch


def ComputeDifferences(diffs):
  """Call ComputePatch on all the Difference objects in 'diffs'."""
  logger.info("%d diffs to compute", len(diffs))

  # Do the largest files first, to try and reduce the long-pole effect.
  by_size = [(i.tf.size, i) for i in diffs]
  by_size.sort(reverse=True)
  by_size = [i[1] for i in by_size]

  lock = threading.Lock()
  diff_iter = iter(by_size)   # accessed under lock

  def worker():
    try:
      lock.acquire()
      for d in diff_iter:
        lock.release()
        start = time.time()
        d.ComputePatch()
        dur = time.time() - start
        lock.acquire()

        tf, sf, patch = d.GetPatch()
        if sf.name == tf.name:
          name = tf.name
        else:
          name = "%s (%s)" % (tf.name, sf.name)
        if patch is None:
          logger.error("patching failed! %40s", name)
        else:
          logger.info(
              "%8.2f sec %8d / %8d bytes (%6.2f%%) %s", dur, len(patch),
              tf.size, 100.0 * len(patch) / tf.size, name)
      lock.release()
    except Exception:
      logger.exception("Failed to compute diff from worker")
      raise

  # start worker threads; wait for them all to finish.
  threads = [threading.Thread(target=worker)
             for i in range(OPTIONS.worker_threads)]
  for th in threads:
    th.start()
  while threads:
    threads.pop().join()


class BlockDifference(object):
  def __init__(self, partition, tgt, src=None, check_first_block=False,
               version=None, disable_imgdiff=False):
    self.tgt = tgt
    self.src = src
    self.partition = partition
    self.check_first_block = check_first_block
    self.disable_imgdiff = disable_imgdiff

    if version is None:
      version = max(
          int(i) for i in
          OPTIONS.info_dict.get("blockimgdiff_versions", "1").split(","))
    assert version >= 3
    self.version = version

    b = blockimgdiff.BlockImageDiff(tgt, src, threads=OPTIONS.worker_threads,
                                    version=self.version,
                                    disable_imgdiff=self.disable_imgdiff)
    self.path = os.path.join(MakeTempDir(), partition)
    b.Compute(self.path)
    self._required_cache = b.max_stashed_size
    self.touched_src_ranges = b.touched_src_ranges
    self.touched_src_sha1 = b.touched_src_sha1

    # On devices with dynamic partitions, for new partitions,
    # src is None but OPTIONS.source_info_dict is not.
    if OPTIONS.source_info_dict is None:
      is_dynamic_build = OPTIONS.info_dict.get(
          "use_dynamic_partitions") == "true"
      is_dynamic_source = False
    else:
      is_dynamic_build = OPTIONS.source_info_dict.get(
          "use_dynamic_partitions") == "true"
      is_dynamic_source = partition in shlex.split(
          OPTIONS.source_info_dict.get("dynamic_partition_list", "").strip())

    is_dynamic_target = partition in shlex.split(
        OPTIONS.info_dict.get("dynamic_partition_list", "").strip())

    # For dynamic partitions builds, check partition list in both source
    # and target build because new partitions may be added, and existing
    # partitions may be removed.
    is_dynamic = is_dynamic_build and (is_dynamic_source or is_dynamic_target)

    if is_dynamic:
      self.device = 'map_partition("%s")' % partition
    else:
      if OPTIONS.source_info_dict is None:
        _, device_path = GetTypeAndDevice("/" + partition, OPTIONS.info_dict)
      else:
        _, device_path = GetTypeAndDevice("/" + partition,
                                          OPTIONS.source_info_dict)
      self.device = '"%s"' % device_path

  @property
  def required_cache(self):
    return self._required_cache

  def WriteScript(self, script, output_zip, progress=None,
                  write_verify_script=False):
    if not self.src:
      # write the output unconditionally
      script.Print("Flashing qassa %s image unconditionally..." % (self.partition,))
    else:
      script.Print("Flashing qassa %s image after verification." % (self.partition,))

    if progress:
      script.ShowProgress(progress, 0)
    self._WriteUpdate(script, output_zip)

    if write_verify_script:
      self.WritePostInstallVerifyScript(script)

  def WriteStrictVerifyScript(self, script):
    """Verify all the blocks in the care_map, including clobbered blocks.

    This differs from the WriteVerifyScript() function: a) it prints different
    error messages; b) it doesn't allow half-way updated images to pass the
    verification."""

    partition = self.partition
    script.Print("Verifying %s..." % (partition,))
    ranges = self.tgt.care_map
    ranges_str = ranges.to_string_raw()
    script.AppendExtra(
        'range_sha1(%s, "%s") == "%s" && ui_print("    Verified.") || '
        'ui_print("%s has unexpected contents.");' % (
            self.device, ranges_str,
            self.tgt.TotalSha1(include_clobbered_blocks=True),
            self.partition))
    script.AppendExtra("")

  def WriteVerifyScript(self, script, touched_blocks_only=False):
    partition = self.partition

    # full OTA
    if not self.src:
      script.Print("Image %s will be patched unconditionally." % (partition,))

    # incremental OTA
    else:
      if touched_blocks_only:
        ranges = self.touched_src_ranges
        expected_sha1 = self.touched_src_sha1
      else:
        ranges = self.src.care_map.subtract(self.src.clobbered_blocks)
        expected_sha1 = self.src.TotalSha1()

      # No blocks to be checked, skipping.
      if not ranges:
        return

      ranges_str = ranges.to_string_raw()
      script.AppendExtra(
          'if (range_sha1(%s, "%s") == "%s" || block_image_verify(%s, '
          'package_extract_file("%s.transfer.list"), "%s.new.dat", '
          '"%s.patch.dat")) then' % (
              self.device, ranges_str, expected_sha1,
              self.device, partition, partition, partition))
      script.Print('Verified %s image...' % (partition,))
      script.AppendExtra('else')

      if self.version >= 4:

        # Bug: 21124327
        # When generating incrementals for the system and vendor partitions in
        # version 4 or newer, explicitly check the first block (which contains
        # the superblock) of the partition to see if it's what we expect. If
        # this check fails, give an explicit log message about the partition
        # having been remounted R/W (the most likely explanation).
        if self.check_first_block:
          script.AppendExtra('check_first_block(%s);' % (self.device,))

        # If version >= 4, try block recovery before abort update
        if partition == "system":
          code = ErrorCode.SYSTEM_RECOVER_FAILURE
        else:
          code = ErrorCode.VENDOR_RECOVER_FAILURE
        script.AppendExtra((
            'ifelse (block_image_recover({device}, "{ranges}") && '
            'block_image_verify({device}, '
            'package_extract_file("{partition}.transfer.list"), '
            '"{partition}.new.dat", "{partition}.patch.dat"), '
            'ui_print("{partition} recovered successfully."), '
            'abort("E{code}: {partition} partition fails to recover"));\n'
            'endif;').format(device=self.device, ranges=ranges_str,
                             partition=partition, code=code))

      # Abort the OTA update. Note that the incremental OTA cannot be applied
      # even if it may match the checksum of the target partition.
      # a) If version < 3, operations like move and erase will make changes
      #    unconditionally and damage the partition.
      # b) If version >= 3, it won't even reach here.
      else:
        if partition == "system":
          code = ErrorCode.SYSTEM_VERIFICATION_FAILURE
        else:
          code = ErrorCode.VENDOR_VERIFICATION_FAILURE
        script.AppendExtra((
            'abort("E%d: %s partition has unexpected contents");\n'
            'endif;') % (code, partition))

  def WritePostInstallVerifyScript(self, script):
    partition = self.partition
    script.Print('Verifying qassa %s image...' % (partition,))
    # Unlike pre-install verification, clobbered_blocks should not be ignored.
    ranges = self.tgt.care_map
    ranges_str = ranges.to_string_raw()
    script.AppendExtra(
        'if range_sha1(%s, "%s") == "%s" then' % (
            self.device, ranges_str,
            self.tgt.TotalSha1(include_clobbered_blocks=True)))

    # Bug: 20881595
    # Verify that extended blocks are really zeroed out.
    if self.tgt.extended:
      ranges_str = self.tgt.extended.to_string_raw()
      script.AppendExtra(
          'if range_sha1(%s, "%s") == "%s" then' % (
              self.device, ranges_str,
              self._HashZeroBlocks(self.tgt.extended.size())))
      script.Print('Verified qassa %s image.' % (partition,))
      if partition == "system":
        code = ErrorCode.SYSTEM_NONZERO_CONTENTS
      else:
        code = ErrorCode.VENDOR_NONZERO_CONTENTS
      script.AppendExtra(
          'else\n'
          '  abort("E%d: %s partition has unexpected non-zero contents after '
          'OTA update");\n'
          'endif;' % (code, partition))
    else:
      script.Print('Verified qassa %s image.' % (partition,))

    if partition == "system":
      code = ErrorCode.SYSTEM_UNEXPECTED_CONTENTS
    else:
      code = ErrorCode.VENDOR_UNEXPECTED_CONTENTS

    script.AppendExtra(
        'else\n'
        '  abort("E%d: %s partition has unexpected contents after OTA '
        'update");\n'
        'endif;' % (code, partition))

  def _WriteUpdate(self, script, output_zip):
    ZipWrite(output_zip,
             '{}.transfer.list'.format(self.path),
             '{}.transfer.list'.format(self.partition))

    # For full OTA, compress the new.dat with brotli with quality 6 to reduce
    # its size. Quailty 9 almost triples the compression time but doesn't
    # further reduce the size too much. For a typical 1.8G system.new.dat
    #                       zip  | brotli(quality 6)  | brotli(quality 9)
    #   compressed_size:    942M | 869M (~8% reduced) | 854M
    #   compression_time:   75s  | 265s               | 719s
    #   decompression_time: 15s  | 25s                | 25s

    if not self.src:
      brotli_cmd = ['brotli', '--quality=6',
                    '--output={}.new.dat.br'.format(self.path),
                    '{}.new.dat'.format(self.path)]
      print("Compressing {}.new.dat with brotli".format(self.partition))
      RunAndCheckOutput(brotli_cmd)

      new_data_name = '{}.new.dat.br'.format(self.partition)
      ZipWrite(output_zip,
               '{}.new.dat.br'.format(self.path),
               new_data_name,
               compress_type=zipfile.ZIP_STORED)
    else:
      new_data_name = '{}.new.dat'.format(self.partition)
      ZipWrite(output_zip, '{}.new.dat'.format(self.path), new_data_name)

    ZipWrite(output_zip,
             '{}.patch.dat'.format(self.path),
             '{}.patch.dat'.format(self.partition),
             compress_type=zipfile.ZIP_STORED)

    if self.partition == "system":
      code = ErrorCode.SYSTEM_UPDATE_FAILURE
    else:
      code = ErrorCode.VENDOR_UPDATE_FAILURE

    call = ('block_image_update({device}, '
            'package_extract_file("{partition}.transfer.list"), '
            '"{new_data_name}", "{partition}.patch.dat") ||\n'
            '  abort("E{code}: Failed to update {partition} image.");\n'
            'delete_recursive("/data/system/package_cache");'.format(
                device=self.device, partition=self.partition,
                new_data_name=new_data_name, code=code))
    script.AppendExtra(script.WordWrap(call))

  def _HashBlocks(self, source, ranges): # pylint: disable=no-self-use
    data = source.ReadRangeSet(ranges)
    ctx = sha1()

    for p in data:
      ctx.update(p)

    return ctx.hexdigest()

  def _HashZeroBlocks(self, num_blocks): # pylint: disable=no-self-use
    """Return the hash value for all zero blocks."""
    zero_block = '\x00' * 4096
    ctx = sha1()
    for _ in range(num_blocks):
      ctx.update(zero_block)

    return ctx.hexdigest()


# This is just a little wrapper around FileSystemDiff.
# It really isn't needed except to maintain consistency with upstream
# BlockDifference and BlockImageDiff.
class FileSystemDifference(object):
  def __init__(self, partition, tgt, src, use_dynamic_partition):
    self.tgt = tgt
    self.src = src
    self.partition = partition
    self.diff = filesystemdiff.FileSystemDiff(partition, tgt, src, threads=OPTIONS.worker_threads, use_dynamic_partition=use_dynamic_partition)

    partition_ = partition
    if partition_ == 'root':
      partition_ = "system"

    if src is None:
      _, self.device = GetTypeAndDevice("/" + partition_, OPTIONS.info_dict)
    else:
      _, self.device = GetTypeAndDevice("/" + partition_,
                                        OPTIONS.source_info_dict)

  def WriteScript(self, script, output_zip, progress=None):
    if progress:
      script.ShowProgress(progress, 0)

    self.diff.Compute(script, output_zip)


DataImage = blockimgdiff.DataImage
EmptyImage = blockimgdiff.EmptyImage

# map recovery.fstab's fs_types to mount/format "partition types"
PARTITION_TYPES = {
    "ext4": "EMMC",
    "emmc": "EMMC",
    "f2fs": "EMMC",
    "squashfs": "EMMC"
}


def GetTypeAndDevice(mount_point, info):
  fstab = info["fstab"]
  if fstab:
    return (PARTITION_TYPES[fstab[mount_point].fs_type],
            fstab[mount_point].device)
  else:
    raise KeyError


def ParseCertificate(data):
  """Parses and converts a PEM-encoded certificate into DER-encoded.

  This gives the same result as `openssl x509 -in <filename> -outform DER`.

  Returns:
    The decoded certificate bytes.
  """
  cert_buffer = []
  save = False
  for line in data.split("\n"):
    if "--END CERTIFICATE--" in line:
      break
    if save:
      cert_buffer.append(line)
    if "--BEGIN CERTIFICATE--" in line:
      save = True
  cert = base64.b64decode("".join(cert_buffer))
  return cert


def ExtractPublicKey(cert):
  """Extracts the public key (PEM-encoded) from the given certificate file.

  Args:
    cert: The certificate filename.

  Returns:
    The public key string.

  Raises:
    AssertionError: On non-zero return from 'openssl'.
  """
  # The behavior with '-out' is different between openssl 1.1 and openssl 1.0.
  # While openssl 1.1 writes the key into the given filename followed by '-out',
  # openssl 1.0 (both of 1.0.1 and 1.0.2) doesn't. So we collect the output from
  # stdout instead.
  cmd = ['openssl', 'x509', '-pubkey', '-noout', '-in', cert]
  proc = Run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  pubkey, stderrdata = proc.communicate()
  assert proc.returncode == 0, \
      'Failed to dump public key from certificate: %s\n%s' % (cert, stderrdata)
  return pubkey


def ExtractAvbPublicKey(key):
  """Extracts the AVB public key from the given public or private key.

  Args:
    key: The input key file, which should be PEM-encoded public or private key.

  Returns:
    The path to the extracted AVB public key file.
  """
  output = MakeTempFile(prefix='avb-', suffix='.avbpubkey')
  RunAndCheckOutput(
      ['avbtool', 'extract_public_key', "--key", key, "--output", output])
  return output


def MakeRecoveryPatch(input_dir, output_sink, recovery_img, boot_img,
                      info_dict=None):
  """Generates the recovery-from-boot patch and writes the script to output.

  Most of the space in the boot and recovery images is just the kernel, which is
  identical for the two, so the resulting patch should be efficient. Add it to
  the output zip, along with a shell script that is run from init.rc on first
  boot to actually do the patching and install the new recovery image.

  Args:
    input_dir: The top-level input directory of the target-files.zip.
    output_sink: The callback function that writes the result.
    recovery_img: File object for the recovery image.
    boot_img: File objects for the boot image.
    info_dict: A dict returned by common.LoadInfoDict() on the input
        target_files. Will use OPTIONS.info_dict if None has been given.
  """
  if info_dict is None:
    info_dict = OPTIONS.info_dict

  full_recovery_image = info_dict.get("full_recovery_image") == "true"
  use_bsdiff = info_dict.get("no_gzip_recovery_ramdisk") == "true"

  if full_recovery_image:
    output_sink("etc/recovery.img", recovery_img.data)

  else:
    system_root_image = info_dict.get("system_root_image") == "true"
    path = os.path.join(input_dir, "SYSTEM", "etc", "recovery-resource.dat")
    # With system-root-image, boot and recovery images will have mismatching
    # entries (only recovery has the ramdisk entry) (Bug: 72731506). Use bsdiff
    # to handle such a case.
    if system_root_image or use_bsdiff:
      diff_program = ["bsdiff"]
      bonus_args = ""
      assert not os.path.exists(path)
    else:
      diff_program = ["imgdiff"]
      if os.path.exists(path):
        diff_program.append("-b")
        diff_program.append(path)
        bonus_args = "--bonus /system/etc/recovery-resource.dat"
      else:
        bonus_args = ""

    d = Difference(recovery_img, boot_img, diff_program=diff_program)
    _, _, patch = d.ComputePatch()
    output_sink("recovery-from-boot.p", patch)

  try:
    # The following GetTypeAndDevice()s need to use the path in the target
    # info_dict instead of source_info_dict.
    boot_type, boot_device = GetTypeAndDevice("/boot", info_dict)
    recovery_type, recovery_device = GetTypeAndDevice("/recovery", info_dict)
  except KeyError:
    return

  if full_recovery_image:
    sh = """#!/system/bin/sh
if ! applypatch --check %(type)s:%(device)s:%(size)d:%(sha1)s; then
  applypatch \\
          --flash /system/etc/recovery.img \\
          --target %(type)s:%(device)s:%(size)d:%(sha1)s && \\
      log -t recovery "Installing new recovery image: succeeded" || \\
      log -t recovery "Installing new recovery image: failed"
else
  log -t recovery "Recovery image already installed"
fi
""" % {'type': recovery_type,
       'device': recovery_device,
       'sha1': recovery_img.sha1,
       'size': recovery_img.size}
  else:
    sh = """#!/system/bin/sh
if ! applypatch --check %(recovery_type)s:%(recovery_device)s:%(recovery_size)d:%(recovery_sha1)s; then
  applypatch %(bonus_args)s \\
          --patch /system/recovery-from-boot.p \\
          --source %(boot_type)s:%(boot_device)s:%(boot_size)d:%(boot_sha1)s \\
          --target %(recovery_type)s:%(recovery_device)s:%(recovery_size)d:%(recovery_sha1)s && \\
      log -t recovery "Installing new recovery image: succeeded" || \\
      log -t recovery "Installing new recovery image: failed"
else
  log -t recovery "Recovery image already installed"
fi
""" % {'boot_size': boot_img.size,
       'boot_sha1': boot_img.sha1,
       'recovery_size': recovery_img.size,
       'recovery_sha1': recovery_img.sha1,
       'boot_type': boot_type,
       'boot_device': boot_device,
       'recovery_type': recovery_type,
       'recovery_device': recovery_device,
       'bonus_args': bonus_args}

  # The install script location moved from /system/etc to /system/bin
  # in the L release.
  sh_location = "bin/install-recovery.sh"

  logger.info("putting script in %s", sh_location)

  output_sink(sh_location, sh.encode())


class DynamicPartitionUpdate(object):
  def __init__(self, src_group=None, tgt_group=None, progress=None,
               block_difference=None):
    self.src_group = src_group
    self.tgt_group = tgt_group
    self.progress = progress
    self.block_difference = block_difference

  @property
  def src_size(self):
    if not self.block_difference:
      return 0
    return DynamicPartitionUpdate._GetSparseImageSize(self.block_difference.src)

  @property
  def tgt_size(self):
    if not self.block_difference:
      return 0
    return DynamicPartitionUpdate._GetSparseImageSize(self.block_difference.tgt)

  @staticmethod
  def _GetSparseImageSize(img):
    if not img:
      return 0
    return img.blocksize * img.total_blocks


class DynamicGroupUpdate(object):
  def __init__(self, src_size=None, tgt_size=None):
    # None: group does not exist. 0: no size limits.
    self.src_size = src_size
    self.tgt_size = tgt_size


class DynamicPartitionsDifference(object):
  def __init__(self, info_dict, block_diffs, progress_dict=None,
               source_info_dict=None, build_without_vendor=False):
    if progress_dict is None:
      progress_dict = dict()

    self._build_without_vendor = build_without_vendor
    self._remove_all_before_apply = False
    if source_info_dict is None:
      self._remove_all_before_apply = True
      source_info_dict = dict()

    block_diff_dict = {e.partition:e for e in block_diffs}
    assert len(block_diff_dict) == len(block_diffs), \
        "Duplicated BlockDifference object for {}".format(
            [partition for partition, count in
             collections.Counter(e.partition for e in block_diffs).items()
             if count > 1])

    self._partition_updates = collections.OrderedDict()

    for p, block_diff in block_diff_dict.items():
      self._partition_updates[p] = DynamicPartitionUpdate()
      self._partition_updates[p].block_difference = block_diff

    for p, progress in progress_dict.items():
      if p in self._partition_updates:
        self._partition_updates[p].progress = progress

    tgt_groups = shlex.split(info_dict.get(
        "super_partition_groups", "").strip())
    src_groups = shlex.split(source_info_dict.get(
        "super_partition_groups", "").strip())

    for g in tgt_groups:
      for p in shlex.split(info_dict.get(
          "super_%s_partition_list" % g, "").strip()):
        assert p in self._partition_updates, \
            "{} is in target super_{}_partition_list but no BlockDifference " \
            "object is provided.".format(p, g)
        self._partition_updates[p].tgt_group = g

    for g in src_groups:
      for p in shlex.split(source_info_dict.get(
          "super_%s_partition_list" % g, "").strip()):
        assert p in self._partition_updates, \
            "{} is in source super_{}_partition_list but no BlockDifference " \
            "object is provided.".format(p, g)
        self._partition_updates[p].src_group = g

    target_dynamic_partitions = set(shlex.split(info_dict.get(
        "dynamic_partition_list", "").strip()))
    block_diffs_with_target = set(p for p, u in self._partition_updates.items()
                                  if u.tgt_size)
    assert block_diffs_with_target == target_dynamic_partitions, \
        "Target Dynamic partitions: {}, BlockDifference with target: {}".format(
            list(target_dynamic_partitions), list(block_diffs_with_target))

    source_dynamic_partitions = set(shlex.split(source_info_dict.get(
        "dynamic_partition_list", "").strip()))
    block_diffs_with_source = set(p for p, u in self._partition_updates.items()
                                  if u.src_size)
    assert block_diffs_with_source == source_dynamic_partitions, \
        "Source Dynamic partitions: {}, BlockDifference with source: {}".format(
            list(source_dynamic_partitions), list(block_diffs_with_source))

    if self._partition_updates:
      logger.info("Updating dynamic partitions %s",
                  self._partition_updates.keys())

    self._group_updates = collections.OrderedDict()

    for g in tgt_groups:
      self._group_updates[g] = DynamicGroupUpdate()
      self._group_updates[g].tgt_size = int(info_dict.get(
          "super_%s_group_size" % g, "0").strip())

    for g in src_groups:
      if g not in self._group_updates:
        self._group_updates[g] = DynamicGroupUpdate()
      self._group_updates[g].src_size = int(source_info_dict.get(
          "super_%s_group_size" % g, "0").strip())

    self._Compute()

  def WriteScript(self, script, output_zip, write_verify_script=False):
    script.Comment('--- Start patching dynamic partitions ---')
    for p, u in self._partition_updates.items():
      if u.src_size and u.tgt_size and u.src_size > u.tgt_size:
        script.Comment('Patch partition %s' % p)
        u.block_difference.WriteScript(script, output_zip, progress=u.progress,
                                       write_verify_script=False)

    op_list_path = MakeTempFile()
    with open(op_list_path, 'w') as f:
      for line in self._op_list:
        f.write('{}\n'.format(line))

    ZipWrite(output_zip, op_list_path, "dynamic_partitions_op_list")

    script.Comment('Update dynamic partition metadata')
    script.AppendExtra('assert(update_dynamic_partitions('
                       'package_extract_file("dynamic_partitions_op_list")));')

    if write_verify_script:
      for p, u in self._partition_updates.items():
        if u.src_size and u.tgt_size and u.src_size > u.tgt_size:
          u.block_difference.WritePostInstallVerifyScript(script)
          script.AppendExtra('unmap_partition("%s");' % p) # ignore errors

    for p, u in self._partition_updates.items():
      if u.tgt_size and u.src_size <= u.tgt_size:
        script.Comment('Patch partition %s' % p)
        u.block_difference.WriteScript(script, output_zip, progress=u.progress,
                                       write_verify_script=write_verify_script)
        if write_verify_script:
          script.AppendExtra('unmap_partition("%s");' % p) # ignore errors

    script.Comment('--- End patching dynamic partitions ---')

  def _Compute(self):
    self._op_list = list()

    def append(line):
      self._op_list.append(line)

    def comment(line):
      self._op_list.append("# %s" % line)

    if self._build_without_vendor:
      comment('System-only build, keep original vendor partition')
      # When building without vendor, we do not want to override
      # any partition already existing. In this case, we can only
      # resize, but not remove / create / re-create any other
      # partition.
      for p, u in self._partition_updates.items():
        comment('Resize partition %s to %s' % (p, u.tgt_size))
        append('resize %s %s' % (p, u.tgt_size))
      return

    if self._remove_all_before_apply:
      comment('Remove all existing dynamic partitions and groups before '
              'applying full OTA')
      append('remove_all_groups')

    for p, u in self._partition_updates.items():
      if u.src_group and not u.tgt_group:
        append('remove %s' % p)

    for p, u in self._partition_updates.items():
      if u.src_group and u.tgt_group and u.src_group != u.tgt_group:
        comment('Move partition %s from %s to default' % (p, u.src_group))
        append('move %s default' % p)

    for p, u in self._partition_updates.items():
      if u.src_size and u.tgt_size and u.src_size > u.tgt_size:
        comment('Shrink partition %s from %d to %d' %
                (p, u.src_size, u.tgt_size))
        append('resize %s %s' % (p, u.tgt_size))

    for g, u in self._group_updates.items():
      if u.src_size is not None and u.tgt_size is None:
        append('remove_group %s' % g)
      if (u.src_size is not None and u.tgt_size is not None and
          u.src_size > u.tgt_size):
        comment('Shrink group %s from %d to %d' % (g, u.src_size, u.tgt_size))
        append('resize_group %s %d' % (g, u.tgt_size))

    for g, u in self._group_updates.items():
      if u.src_size is None and u.tgt_size is not None:
        comment('Add group %s with maximum size %d' % (g, u.tgt_size))
        append('add_group %s %d' % (g, u.tgt_size))
      if (u.src_size is not None and u.tgt_size is not None and
          u.src_size < u.tgt_size):
        comment('Grow group %s from %d to %d' % (g, u.src_size, u.tgt_size))
        append('resize_group %s %d' % (g, u.tgt_size))

    for p, u in self._partition_updates.items():
      if u.tgt_group and not u.src_group:
        comment('Add partition %s to group %s' % (p, u.tgt_group))
        append('add %s %s' % (p, u.tgt_group))

    for p, u in self._partition_updates.items():
      if u.tgt_size and u.src_size < u.tgt_size:
        comment('Grow partition %s from %d to %d' % (p, u.src_size, u.tgt_size))
        append('resize %s %d' % (p, u.tgt_size))

    for p, u in self._partition_updates.items():
      if u.src_group and u.tgt_group and u.src_group != u.tgt_group:
        comment('Move partition %s from default to %s' %
                (p, u.tgt_group))
        append('move %s %s' % (p, u.tgt_group))
