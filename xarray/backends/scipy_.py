import gzip
import io
import os

import numpy as np

from ..core.indexing import NumpyIndexingAdapter
from ..core.utils import (
    Frozen,
    FrozenDict,
    close_on_error,
    try_read_magic_number_from_file_or_path,
)
from ..core.variable import Variable
from .common import (
    BACKEND_ENTRYPOINTS,
    BackendArray,
    BackendEntrypoint,
    WritableCFDataStore,
    _normalize_path,
)
from .file_manager import CachingFileManager, DummyFileManager
from .locks import ensure_lock, get_write_lock
from .netcdf3 import encode_nc3_attr_value, encode_nc3_variable, is_valid_nc3_name
from .store import StoreBackendEntrypoint

try:
    import scipy.io

    has_scipy = True
except ModuleNotFoundError:
    has_scipy = False


def _decode_string(s):
    if isinstance(s, bytes):
        return s.decode("utf-8", "replace")
    return s


def _decode_attrs(d):
    # don't decode _FillValue from bytes -> unicode, because we want to ensure
    # that its type matches the data exactly
    return {k: v if k == "_FillValue" else _decode_string(v) for (k, v) in d.items()}


class ScipyArrayWrapper(BackendArray):
    def __init__(self, variable_name, datastore):
        self.datastore = datastore
        self.variable_name = variable_name
        array = self.get_variable().data
        self.shape = array.shape
        self.dtype = np.dtype(array.dtype.kind + str(array.dtype.itemsize))

    def get_variable(self, needs_lock=True):
        ds = self.datastore._manager.acquire(needs_lock)
        return ds.variables[self.variable_name]

    def __getitem__(self, key):
        data = NumpyIndexingAdapter(self.get_variable().data)[key]
        # Copy data if the source file is mmapped. This makes things consistent
        # with the netCDF4 library by ensuring we can safely read arrays even
        # after closing associated files.
        copy = self.datastore.ds.use_mmap
        return np.array(data, dtype=self.dtype, copy=copy)

    def __setitem__(self, key, value):
        with self.datastore.lock:
            data = self.get_variable(needs_lock=False)
            try:
                data[key] = value
            except TypeError:
                if key is Ellipsis:
                    # workaround for GH: scipy/scipy#6880
                    data[:] = value
                else:
                    raise


def _open_scipy_netcdf(filename, mode, mmap, version):
    # if the string ends with .gz, then gunzip and open as netcdf file
    if isinstance(filename, str) and filename.endswith(".gz"):
        try:
            return scipy.io.netcdf_file(
                gzip.open(filename), mode=mode, mmap=mmap, version=version
            )
        except TypeError as e:
            # TODO: gzipped loading only works with NetCDF3 files.
            errmsg = e.args[0]
            if "is not a valid NetCDF 3 file" in errmsg:
                raise ValueError("gzipped file loading only supports NetCDF 3 files.")
            else:
                raise

    if isinstance(filename, bytes) and filename.startswith(b"CDF"):
        # it's a NetCDF3 bytestring
        filename = io.BytesIO(filename)

    try:
        return scipy.io.netcdf_file(filename, mode=mode, mmap=mmap, version=version)
    except TypeError as e:  # netcdf3 message is obscure in this case
        errmsg = e.args[0]
        if "is not a valid NetCDF 3 file" in errmsg:
            msg = """
            If this is a NetCDF4 file, you may need to install the
            netcdf4 library, e.g.,

            $ pip install netcdf4
            """
            errmsg += msg
            raise TypeError(errmsg)
        else:
            raise


class ScipyDataStore(WritableCFDataStore):
    """Store for reading and writing data via scipy.io.netcdf.

    This store has the advantage of being able to be initialized with a
    StringIO object, allow for serialization without writing to disk.

    It only supports the NetCDF3 file-format.
    """

    def __init__(
        self, filename_or_obj, mode="r", format=None, group=None, mmap=None, lock=None
    ):
        if group is not None:
            raise ValueError("cannot save to a group with the scipy.io.netcdf backend")

        if format is None or format == "NETCDF3_64BIT":
            version = 2
        elif format == "NETCDF3_CLASSIC":
            version = 1
        else:
            raise ValueError(f"invalid format for scipy.io.netcdf backend: {format!r}")

        if lock is None and mode != "r" and isinstance(filename_or_obj, str):
            lock = get_write_lock(filename_or_obj)

        self.lock = ensure_lock(lock)

        if isinstance(filename_or_obj, str):
            manager = CachingFileManager(
                _open_scipy_netcdf,
                filename_or_obj,
                mode=mode,
                lock=lock,
                kwargs=dict(mmap=mmap, version=version),
            )
        else:
            scipy_dataset = _open_scipy_netcdf(
                filename_or_obj, mode=mode, mmap=mmap, version=version
            )
            manager = DummyFileManager(scipy_dataset)

        self._manager = manager

    @property
    def ds(self):
        return self._manager.acquire()

    def open_store_variable(self, name, var):
        return Variable(
            var.dimensions,
            ScipyArrayWrapper(name, self),
            _decode_attrs(var._attributes),
        )

    def get_variables(self):
        return FrozenDict(
            (k, self.open_store_variable(k, v)) for k, v in self.ds.variables.items()
        )

    def get_attrs(self):
        return Frozen(_decode_attrs(self.ds._attributes))

    def get_dimensions(self):
        return Frozen(self.ds.dimensions)

    def get_encoding(self):
        return {
            "unlimited_dims": {k for k, v in self.ds.dimensions.items() if v is None}
        }

    def set_dimension(self, name, length, is_unlimited=False):
        if name in self.ds.dimensions:
            raise ValueError(
                f"{type(self).__name__} does not support modifying dimensions"
            )
        dim_length = length if not is_unlimited else None
        self.ds.createDimension(name, dim_length)

    def _validate_attr_key(self, key):
        if not is_valid_nc3_name(key):
            raise ValueError("Not a valid attribute name")

    def set_attribute(self, key, value):
        self._validate_attr_key(key)
        value = encode_nc3_attr_value(value)
        setattr(self.ds, key, value)

    def encode_variable(self, variable):
        variable = encode_nc3_variable(variable)
        return variable

    def prepare_variable(
        self, name, variable, check_encoding=False, unlimited_dims=None
    ):
        if (
            check_encoding
            and variable.encoding
            and variable.encoding != {"_FillValue": None}
        ):
            raise ValueError(
                f"unexpected encoding for scipy backend: {list(variable.encoding)}"
            )

        data = variable.data
        # nb. this still creates a numpy array in all memory, even though we
        # don't write the data yet; scipy.io.netcdf does not not support
        # incremental writes.
        if name not in self.ds.variables:
            self.ds.createVariable(name, data.dtype, variable.dims)
        scipy_var = self.ds.variables[name]
        for k, v in variable.attrs.items():
            self._validate_attr_key(k)
            setattr(scipy_var, k, v)

        target = ScipyArrayWrapper(name, self)

        return target, data

    def sync(self):
        self.ds.sync()

    def close(self):
        self._manager.close()


class ScipyBackendEntrypoint(BackendEntrypoint):
    available = has_scipy

    def guess_can_open(self, filename_or_obj):

        magic_number = try_read_magic_number_from_file_or_path(filename_or_obj)
        if magic_number is not None and magic_number.startswith(b"\x1f\x8b"):
            with gzip.open(filename_or_obj) as f:
                magic_number = try_read_magic_number_from_file_or_path(f)
        if magic_number is not None:
            return magic_number.startswith(b"CDF")

        try:
            _, ext = os.path.splitext(filename_or_obj)
        except TypeError:
            return False
        return ext in {".nc", ".nc4", ".cdf", ".gz"}

    def open_dataset(
        self,
        filename_or_obj,
        mask_and_scale=True,
        decode_times=True,
        concat_characters=True,
        decode_coords=True,
        drop_variables=None,
        use_cftime=None,
        decode_timedelta=None,
        mode="r",
        format=None,
        group=None,
        mmap=None,
        lock=None,
    ):

        filename_or_obj = _normalize_path(filename_or_obj)
        store = ScipyDataStore(
            filename_or_obj, mode=mode, format=format, group=group, mmap=mmap, lock=lock
        )

        store_entrypoint = StoreBackendEntrypoint()
        with close_on_error(store):
            ds = store_entrypoint.open_dataset(
                store,
                mask_and_scale=mask_and_scale,
                decode_times=decode_times,
                concat_characters=concat_characters,
                decode_coords=decode_coords,
                drop_variables=drop_variables,
                use_cftime=use_cftime,
                decode_timedelta=decode_timedelta,
            )
        return ds


BACKEND_ENTRYPOINTS["scipy"] = ScipyBackendEntrypoint
