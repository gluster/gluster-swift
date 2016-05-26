# Copyright (c) 2012-2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import stat
import json
import errno
import random
import logging
from hashlib import md5
from eventlet import sleep
import cPickle as pickle
from cStringIO import StringIO
import pickletools
from gluster.swift.common.exceptions import GlusterFileSystemIOError
from swift.common.exceptions import DiskFileNoSpace
from swift.common.db import utf8encodekeys
from gluster.swift.common.fs_utils import do_getctime, do_getmtime, do_stat, \
    do_rmdir, do_log_rl, get_filename_from_fd, do_open, do_getsize, \
    do_getxattr, do_setxattr, do_removexattr, do_read, do_close, do_dup, \
    do_lseek, do_fstat, do_fsync, do_rename
from gluster.swift.common import Glusterfs

try:
    import scandir
    scandir_present = True
except ImportError:
    scandir_present = False


X_CONTENT_TYPE = 'Content-Type'
X_CONTENT_LENGTH = 'Content-Length'
X_TIMESTAMP = 'X-Timestamp'
X_PUT_TIMESTAMP = 'X-PUT-Timestamp'
X_TYPE = 'X-Type'
X_ETAG = 'ETag'
X_OBJECTS_COUNT = 'X-Object-Count'
X_BYTES_USED = 'X-Bytes-Used'
X_CONTAINER_COUNT = 'X-Container-Count'
X_OBJECT_TYPE = 'X-Object-Type'
DIR_TYPE = 'application/directory'
ACCOUNT = 'Account'
METADATA_KEY = 'user.swift.metadata'
MAX_XATTR_SIZE = 65536
CONTAINER = 'container'
DIR_NON_OBJECT = 'dir'
DIR_OBJECT = 'marker_dir'
TEMP_DIR = 'tmp'
ASYNCDIR = 'async_pending'  # Keep in sync with swift.obj.server.ASYNCDIR
TRASHCAN = '.trashcan'
FILE = 'file'
FILE_TYPE = 'application/octet-stream'
OBJECT = 'Object'
DEFAULT_UID = -1
DEFAULT_GID = -1
PICKLE_PROTOCOL = 2
CHUNK_SIZE = 65536


class SafeUnpickler(object):
    """
    Loading a pickled stream is potentially unsafe and exploitable because
    the loading process can import modules/classes (via GLOBAL opcode) and
    run any callable (via REDUCE opcode). As the metadata stored in Swift
    is just a dictionary, we take away these powerful "features", thus
    making the loading process safe. Hence, this is very Swift specific
    and is not a general purpose safe unpickler.
    """

    __slots__ = 'OPCODE_BLACKLIST'
    OPCODE_BLACKLIST = ('GLOBAL', 'REDUCE', 'BUILD', 'OBJ', 'NEWOBJ', 'INST',
                        'EXT1', 'EXT2', 'EXT4')

    @classmethod
    def find_class(self, module, name):
        # Do not allow importing of ANY module. This is really redundant as
        # we block those OPCODEs that results in invocation of this method.
        raise pickle.UnpicklingError('Potentially unsafe pickle')

    @classmethod
    def loads(self, string):
        for opcode in pickletools.genops(string):
            if opcode[0].name in self.OPCODE_BLACKLIST:
                raise pickle.UnpicklingError('Potentially unsafe pickle')
        orig_unpickler = pickle.Unpickler(StringIO(string))
        orig_unpickler.find_global = self.find_class
        return orig_unpickler.load()


pickle.loads = SafeUnpickler.loads


def normalize_timestamp(timestamp):
    """
    Format a timestamp (string or numeric) into a standardized
    xxxxxxxxxx.xxxxx (10.5) format.

    Note that timestamps using values greater than or equal to November 20th,
    2286 at 17:46 UTC will use 11 digits to represent the number of
    seconds.

    :param timestamp: unix timestamp
    :returns: normalized timestamp as a string
    """
    return "%016.05f" % (float(timestamp))


def serialize_metadata(metadata):
    return json.dumps(metadata, separators=(',', ':'))


def deserialize_metadata(metastr):
    """
    Returns dict populated with metadata if deserializing is successful.
    Returns empty dict if deserialzing fails.
    """
    if metastr.startswith('\x80\x02}') and metastr.endswith('.') and \
            Glusterfs._read_pickled_metadata:
        # Assert that the serialized metadata is pickled using
        # pickle protocol 2 and is a dictionary.
        try:
            return pickle.loads(metastr)
        except Exception:
            logging.warning("pickle.loads() failed.", exc_info=True)
            return {}
    elif metastr.startswith('{') and metastr.endswith('}'):

        def _list_to_tuple(d):
            for k, v in d.iteritems():
                if isinstance(v, list):
                    d[k] = tuple(i.encode('utf-8')
                                 if isinstance(i, unicode) else i for i in v)
                if isinstance(v, unicode):
                    d[k] = v.encode('utf-8')
            return d

        try:
            metadata = json.loads(metastr, object_hook=_list_to_tuple)
            utf8encodekeys(metadata)
            return metadata
        except (UnicodeDecodeError, ValueError):
            logging.warning("json.loads() failed.", exc_info=True)
            return {}
    else:
        logging.warning("Invalid metadata format (neither PICKLE nor JSON)")
        return {}


def read_metadata(path_or_fd):
    """
    Helper function to read the serialized metadata from a File/Directory.

    :param path_or_fd: File/Directory path or fd from which to read metadata.

    :returns: dictionary of metadata
    """
    metastr = ''
    key = 0
    try:
        while True:
            metastr += do_getxattr(path_or_fd, '%s%s' %
                                   (METADATA_KEY, (key or '')))
            key += 1
            if len(metastr) < MAX_XATTR_SIZE:
                # Prevent further getxattr calls
                break
    except IOError as err:
        if err.errno != errno.ENODATA:
            raise

    if not metastr:
        return {}

    metadata = deserialize_metadata(metastr)
    if not metadata:
        # Empty dict i.e deserializing of metadata has failed, probably
        # because it is invalid or incomplete or corrupt
        clean_metadata(path_or_fd)

    assert isinstance(metadata, dict)
    return metadata


def write_metadata(path_or_fd, metadata):
    """
    Helper function to write serialized metadata for a File/Directory.

    :param path_or_fd: File/Directory path or fd to write the metadata
    :param metadata: dictionary of metadata write
    """
    assert isinstance(metadata, dict)
    metastr = serialize_metadata(metadata)
    key = 0
    while metastr:
        try:
            do_setxattr(path_or_fd,
                        '%s%s' % (METADATA_KEY, key or ''),
                        metastr[:MAX_XATTR_SIZE])
        except IOError as err:
            if err.errno in (errno.ENOSPC, errno.EDQUOT):
                if isinstance(path_or_fd, int):
                    filename = get_filename_from_fd(path_or_fd)
                    do_log_rl("write_metadata(%d, metadata) failed: %s : %s",
                              path_or_fd, err, filename)
                else:
                    do_log_rl("write_metadata(%s, metadata) failed: %s",
                              path_or_fd, err)
                raise DiskFileNoSpace()
            else:
                raise GlusterFileSystemIOError(
                    err.errno,
                    'setxattr("%s", %s, metastr)' % (path_or_fd, key))
        metastr = metastr[MAX_XATTR_SIZE:]
        key += 1


def clean_metadata(path_or_fd):
    key = 0
    while True:
        try:
            do_removexattr(path_or_fd, '%s%s' % (METADATA_KEY, (key or '')))
        except IOError as err:
            if err.errno == errno.ENODATA:
                break
            raise GlusterFileSystemIOError(
                err.errno, 'removexattr("%s", %s)' % (path_or_fd, key))
        key += 1


def validate_container(metadata):
    if not metadata:
        logging.warn('validate_container: No metadata')
        return False

    if X_TYPE not in metadata.keys() or \
       X_TIMESTAMP not in metadata.keys() or \
       X_PUT_TIMESTAMP not in metadata.keys() or \
       X_OBJECTS_COUNT not in metadata.keys() or \
       X_BYTES_USED not in metadata.keys():
        return False

    (value, timestamp) = metadata[X_TYPE]
    if value == CONTAINER:
        return True

    logging.warn('validate_container: metadata type is not CONTAINER (%r)',
                 value)
    return False


def validate_account(metadata):
    if not metadata:
        logging.warn('validate_account: No metadata')
        return False

    if X_TYPE not in metadata.keys() or \
       X_TIMESTAMP not in metadata.keys() or \
       X_PUT_TIMESTAMP not in metadata.keys() or \
       X_OBJECTS_COUNT not in metadata.keys() or \
       X_BYTES_USED not in metadata.keys() or \
       X_CONTAINER_COUNT not in metadata.keys():
        return False

    (value, timestamp) = metadata[X_TYPE]
    if value == ACCOUNT:
        return True

    logging.warn('validate_account: metadata type is not ACCOUNT (%r)',
                 value)
    return False


def validate_object(metadata, statinfo=None):
    if not metadata:
        return False

    if X_TIMESTAMP not in metadata.keys() or \
       X_CONTENT_TYPE not in metadata.keys() or \
       X_ETAG not in metadata.keys() or \
       X_CONTENT_LENGTH not in metadata.keys() or \
       X_TYPE not in metadata.keys() or \
       X_OBJECT_TYPE not in metadata.keys():
        return False

    if statinfo and stat.S_ISREG(statinfo.st_mode):

        # File length has changed.
        if int(metadata[X_CONTENT_LENGTH]) != statinfo.st_size:
            return False

        # TODO: Handle case where file content has changed but the length
        # remains the same.

    if metadata[X_TYPE] == OBJECT:
        return True

    logging.warn('validate_object: metadata type is not OBJECT (%r)',
                 metadata[X_TYPE])
    return False


def _update_list(path, cont_path, src_list, reg_file=True, object_count=0,
                 bytes_used=0, obj_list=[]):
    if isinstance(path, unicode):
        path = path.encode('utf-8')
    # strip the prefix off, also stripping the leading and trailing slashes
    obj_path = path.replace(cont_path, '').strip(os.path.sep)

    for obj_name in src_list:
        # If it is not a reg_file then it is a directory.
        if not reg_file and not Glusterfs._implicit_dir_objects:
            # Now check if this is a dir object or a gratuiously crated
            # directory
            try:
                metadata = \
                    read_metadata(os.path.join(cont_path, obj_path, obj_name))
            except GlusterFileSystemIOError as err:
                if err.errno in (errno.ENOENT, errno.ESTALE):
                    # object might have been deleted by another process
                    # since the src_list was originally built
                    continue
                else:
                    raise err
            if not dir_is_object(metadata):
                continue

        if obj_path:
            obj_list.append(os.path.join(obj_path, obj_name))
        else:
            obj_list.append(obj_name)

        object_count += 1

        if reg_file and Glusterfs._do_getsize:
            bytes_used += do_getsize(os.path.join(path, obj_name))
            sleep()

    return object_count, bytes_used


def update_list(path, cont_path, dirs=[], files=[], object_count=0,
                bytes_used=0, obj_list=[]):
    if files:
        object_count, bytes_used = _update_list(path, cont_path, files, True,
                                                object_count, bytes_used,
                                                obj_list)
    if dirs:
        object_count, bytes_used = _update_list(path, cont_path, dirs, False,
                                                object_count, bytes_used,
                                                obj_list)
    return object_count, bytes_used


def get_container_details(cont_path):
    """
    get container details by traversing the filesystem
    """
    bytes_used = 0
    object_count = 0
    obj_list = []

    for (path, dirs, files) in gf_walk(cont_path):
        object_count, bytes_used = update_list(path, cont_path, dirs,
                                               files, object_count,
                                               bytes_used, obj_list)

        sleep()

    return obj_list, object_count, bytes_used


def list_objects_gsexpiring_container(container_path):
    """
    This method does a simple walk, unlike get_container_details which
    walks the filesystem tree and does a getxattr() on every directory
    to check if it's a directory marker object and stat() on every file
    to get it's size. These are not required for gsexpiring volume as
    it can never have directory marker objects in it and all files are
    zero-byte in size.
    """
    obj_list = []

    for (root, dirs, files) in os.walk(container_path):
        for f in files:
            obj_path = os.path.join(root, f)
            obj = obj_path[(len(container_path) + 1):]
            obj_list.append(obj)
        # Yield the co-routine cooperatively
        sleep()

    return obj_list


def delete_tracker_object(container_path, obj):
    """
    Delete zero-byte tracker object from gsexpiring volume.
    Called by:
        - gluster.swift.obj.expirer.ObjectExpirer.pop_queue()
        - gluster.swift.common.DiskDir.DiskDir.delete_object()
    """
    tracker_object_path = os.path.join(container_path, obj)

    try:
        os.unlink(tracker_object_path)
    except OSError as err:
        if err.errno in (errno.ENOENT, errno.ESTALE):
            # Ignore removal from another entity.
            return
        elif err.errno == errno.EISDIR:
            # Handle race: Was a file during crawl, but now it's a
            # directory. There are no 'directory marker' objects in
            # gsexpiring volume.
            return
        else:
            raise

    # This part of code is very similar to DiskFile._unlinkold()
    dirname = os.path.dirname(tracker_object_path)
    while dirname and dirname != container_path:
        if not rmobjdir(dirname, marker_dir_check=False):
            # If a directory with objects has been found, we can stop
            # garbage collection
            break
        else:
            # Traverse upwards till the root of container
            dirname = os.path.dirname(dirname)


def get_account_details(acc_path):
    """
    Return container_list and container_count.
    """
    container_list = []

    for entry in gf_listdir(acc_path):
        if entry.is_dir() and \
                entry.name not in (TEMP_DIR, ASYNCDIR, TRASHCAN):
            container_list.append(entry.name)

    return container_list, len(container_list)


def _read_for_etag(fp):
    etag = md5()
    while True:
        chunk = do_read(fp, CHUNK_SIZE)
        if chunk:
            etag.update(chunk)
            if len(chunk) >= CHUNK_SIZE:
                # It is likely that we have more data to be read from the
                # file. Yield the co-routine cooperatively to avoid
                # consuming the worker during md5sum() calculations on
                # large files.
                sleep()
        else:
            break
    return etag.hexdigest()


def _get_etag(path_or_fd):
    """
    FIXME: It would be great to have a translator that returns the md5sum() of
    the file as an xattr that can be simply fetched.

    Since we don't have that we should yield after each chunk read and
    computed so that we don't consume the worker thread.
    """
    etag = ''
    if isinstance(path_or_fd, int):
        # We are given a file descriptor, so this is an invocation from the
        # DiskFile.open() method.
        fd = path_or_fd
        dup_fd = do_dup(fd)
        try:
            etag = _read_for_etag(dup_fd)
            do_lseek(fd, 0, os.SEEK_SET)
        finally:
            do_close(dup_fd)
    else:
        # We are given a path to the object when the DiskDir.list_objects_iter
        # method invokes us.
        path = path_or_fd
        fd = do_open(path, os.O_RDONLY)
        try:
            etag = _read_for_etag(fd)
        finally:
            do_close(fd)

    return etag


def get_object_metadata(obj_path_or_fd, stats=None):
    """
    Return metadata of object.
    """
    if not stats:
        if isinstance(obj_path_or_fd, int):
            # We are given a file descriptor, so this is an invocation from the
            # DiskFile.open() method.
            stats = do_fstat(obj_path_or_fd)
        else:
            # We are given a path to the object when the
            # DiskDir.list_objects_iter method invokes us.
            stats = do_stat(obj_path_or_fd)

    if not stats:
        metadata = {}
    else:
        is_dir = stat.S_ISDIR(stats.st_mode)
        metadata = {
            X_TYPE: OBJECT,
            X_TIMESTAMP: normalize_timestamp(stats.st_ctime),
            X_CONTENT_TYPE: DIR_TYPE if is_dir else FILE_TYPE,
            X_OBJECT_TYPE: DIR_NON_OBJECT if is_dir else FILE,
            X_CONTENT_LENGTH: 0 if is_dir else stats.st_size,
            X_ETAG: md5().hexdigest() if is_dir else _get_etag(obj_path_or_fd)}
    return metadata


def _add_timestamp(metadata_i):
    # At this point we have a simple key/value dictionary, turn it into
    # key/(value,timestamp) pairs.
    timestamp = 0
    metadata = {}
    for key, value_i in metadata_i.iteritems():
        if not isinstance(value_i, tuple):
            metadata[key] = (value_i, timestamp)
        else:
            metadata[key] = value_i
    return metadata


def get_container_metadata(cont_path):
    objects = []
    object_count = 0
    bytes_used = 0
    objects, object_count, bytes_used = get_container_details(cont_path)
    metadata = {X_TYPE: CONTAINER,
                X_TIMESTAMP: normalize_timestamp(
                    do_getctime(cont_path)),
                X_PUT_TIMESTAMP: normalize_timestamp(
                    do_getmtime(cont_path)),
                X_OBJECTS_COUNT: object_count,
                X_BYTES_USED: bytes_used}
    return _add_timestamp(metadata)


def get_account_metadata(acc_path):
    containers = []
    container_count = 0
    containers, container_count = get_account_details(acc_path)
    metadata = {X_TYPE: ACCOUNT,
                X_TIMESTAMP: normalize_timestamp(
                    do_getctime(acc_path)),
                X_PUT_TIMESTAMP: normalize_timestamp(
                    do_getmtime(acc_path)),
                X_OBJECTS_COUNT: 0,
                X_BYTES_USED: 0,
                X_CONTAINER_COUNT: container_count}
    return _add_timestamp(metadata)


def restore_metadata(path, metadata, meta_orig):
    if not meta_orig:
        # Container and account metadata
        meta_orig = read_metadata(path)
    if meta_orig:
        meta_new = meta_orig.copy()
        meta_new.update(metadata)
    else:
        meta_new = metadata
    if meta_orig != meta_new:
        write_metadata(path, meta_new)
    return meta_new


def create_object_metadata(obj_path_or_fd, stats=None, existing_meta={}):
    # We must accept either a path or a file descriptor as an argument to this
    # method, as the diskfile modules uses a file descriptior and the DiskDir
    # module (for container operations) uses a path.
    metadata_from_stat = get_object_metadata(obj_path_or_fd, stats)
    return restore_metadata(obj_path_or_fd, metadata_from_stat, existing_meta)


def create_container_metadata(cont_path):
    metadata = get_container_metadata(cont_path)
    rmd = restore_metadata(cont_path, metadata, {})
    return rmd


def create_account_metadata(acc_path):
    metadata = get_account_metadata(acc_path)
    rmd = restore_metadata(acc_path, metadata, {})
    return rmd


# The following dir_xxx calls should definitely be replaced
# with a Metadata class to encapsulate their implementation.
# :FIXME: For now we have them as functions, but we should
# move them to a class.
def dir_is_object(metadata):
    """
    Determine if the directory with the path specified
    has been identified as an object
    """
    return metadata.get(X_OBJECT_TYPE, "") == DIR_OBJECT


def rmobjdir(dir_path, marker_dir_check=True):
    """
    Removes the directory as long as there are no objects stored in it. This
    works for containers also.
    """
    try:
        do_rmdir(dir_path)
    except OSError as err:
        if err.errno in (errno.ENOENT, errno.ESTALE):
            # No such directory exists
            return False
        if err.errno != errno.ENOTEMPTY:
            raise
        # Handle this non-empty directories below.
    else:
        return True

    # We have a directory that is not empty, walk it to see if it is filled
    # with empty sub-directories that are not user created objects
    # (gratuitously created as a result of other object creations).
    for (path, dirs, files) in gf_walk(dir_path, topdown=False):
        for directory in dirs:
            fullpath = os.path.join(path, directory)

            if marker_dir_check:
                try:
                    metadata = read_metadata(fullpath)
                except GlusterFileSystemIOError as err:
                    if err.errno in (errno.ENOENT, errno.ESTALE):
                        # Ignore removal from another entity.
                        continue
                    raise
                else:
                    if dir_is_object(metadata):
                        # Wait, this is an object created by the caller
                        # We cannot delete
                        return False

            # Directory is not an object created by the caller
            # so we can go ahead and delete it.
            try:
                do_rmdir(fullpath)
            except OSError as err:
                if err.errno == errno.ENOTEMPTY:
                    # Directory is not empty, it might have objects in it
                    return False
                if err.errno in (errno.ENOENT, errno.ESTALE):
                    # No such directory exists, already removed, ignore
                    continue
                raise

    try:
        do_rmdir(dir_path)
    except OSError as err:
        if err.errno == errno.ENOTEMPTY:
            # Directory is not empty, race with object creation
            return False
        if err.errno in (errno.ENOENT, errno.ESTALE):
            # No such directory exists, already removed, ignore
            return True
        raise
    else:
        return True


def write_pickle(obj, dest, tmp=None, pickle_protocol=0):
    """
    Ensure that a pickle file gets written to disk.  The file is first written
    to a tmp file location in the destination directory path, ensured it is
    synced to disk, then moved to its final destination name.

    This version takes advantage of Gluster's dot-prefix-dot-suffix naming
    where the a file named ".thefile.name.9a7aasv" is hashed to the same
    Gluster node as "thefile.name". This ensures the renaming of a temp file
    once written does not move it to another Gluster node.

    :param obj: python object to be pickled
    :param dest: path of final destination file
    :param tmp: path to tmp to use, defaults to None (ignored)
    :param pickle_protocol: protocol to pickle the obj with, defaults to 0
    """
    dirname = os.path.dirname(dest)
    # Create destination directory
    try:
        os.makedirs(dirname)
    except OSError as err:
        if err.errno != errno.EEXIST:
            raise
    basename = os.path.basename(dest)
    tmpname = '.' + basename + '.' + \
        md5(basename + str(random.random())).hexdigest()
    tmppath = os.path.join(dirname, tmpname)
    with open(tmppath, 'wb') as fo:
        pickle.dump(obj, fo, pickle_protocol)
        # TODO: This flush() method call turns into a flush() system call
        # We'll need to wrap this as well, but we would do this by writing
        # a context manager for our own open() method which returns an object
        # in fo which makes the gluster API call.
        fo.flush()
        do_fsync(fo)
    do_rename(tmppath, dest)


DT_UNKNOWN = 0
DT_DIR = 4


class SmallDirEntry(object):
    """
    This class is an essential subset of DirEntry class from Python 3.5
    https://docs.python.org/3.5/library/os.html#os.DirEntry
    """
    __slots__ = ('name', '_d_type', '_path', '_stat')

    def __init__(self, path, name, d_type):
        self.name = name
        self._path = path  # Parent directory
        self._d_type = d_type
        self._stat = None  # Populated only if is_dir() is invoked.

    def is_dir(self):
        if self._d_type == DT_UNKNOWN:
            try:
                if not self._stat:
                    self._stat = os.lstat(os.path.join(self._path, self.name))
            except OSError as err:
                if err.errno != errno.ENOENT:
                    raise
                return False
            return stat.S_ISDIR(self._stat.st_mode)
        else:
            return self._d_type == DT_DIR


def gf_listdir(path):
    if scandir_present:
        for entry in scandir.scandir(path):
            yield entry
    else:
        for name in os.listdir(path):
            yield SmallDirEntry(path, name, DT_UNKNOWN)


def _walk(top, topdown=True, onerror=None, followlinks=False):
    """
    This is very similar to os.walk() implementation present in Python 2.7.11
    (https://hg.python.org/cpython/file/v2.7.11/Lib/os.py#l209) but with the
    following differences:
        * This internally uses scandir which is a directory iterator instead
          of os.listdir which returns an actual list.
        * Makes no stat() calls. To do so, it deliberately chooses to ignore
          https://bugs.python.org/issue23605#msg237775 issue.
    """
    dirs = []  # List of DirEntry objects
    nondirs = []  # List of names (strings)

    try:
        for entry in scandir.scandir(top):
            if entry.is_dir():
                dirs.append(entry)
            else:
                nondirs.append(entry.name)
    except OSError as err:
        # As entries_iter is not a list and is a true iterator, it can raise
        # an exception and blow-up. The try-except block here is to handle it
        # gracefully and return.
        if onerror is not None:
            onerror(err)
        return

    if topdown:
        yield top, [d.name for d in dirs], nondirs

    for directory in dirs:
        if followlinks or not directory.is_symlink():
            new_path = os.path.join(top, directory.name)
            for x in _walk(new_path, topdown, onerror, followlinks):
                yield x

    if not topdown:
        yield top, [d.name for d in dirs], nondirs


if scandir_present:
    gf_walk = _walk
else:
    gf_walk = os.walk
