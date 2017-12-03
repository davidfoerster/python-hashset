import sys, math, itertools
import struct, pickle
import hashset.util as util
from .util import property_setter
from .util.math import ceil_div, is_pow2, ceil_pow2
from .util.iter import stareach


class _vardata_hook:
	def __init__( self, name ):
		self.name = '_' + name
		self.fset = None

	def setter( self, fset ):
		self.fset = fset
		return self

	def __get__( self, instance, owner ):
		return getattr(instance, self.name)

	def __set__( self, instance, value ):
		old_value = self.__get__(instance, None)
		if type(value) is not type(old_value) or value != old_value:
			if self.fset is None:
				setattr(instance, self.name, value)
			else:
				self.fset(instance, value)

			instance._vardata = None


class header:
	byteorder = sys.byteorder
	_magic = b'hashset '
	_version = 1

	_struct = struct.Struct('=BB 2x I')
	_struct_keys = ('version', 'int_size', 'index_offset')
	_vardata_keys = ('element_count', 'bucket_count', 'hasher', 'pickler')
	vars().update({ k: _vardata_hook(k) for k in _vardata_keys[1:] })


	def __init__( self, hasher, pickler, int_size=8 ):
		self.int_size = int_size
		self.index_offset = None

		self._vardata = None
		self._hasher = hasher
		self._pickler = pickler
		self._element_count = None
		self._bucket_count = None
		self._bucket_mask = None


	@property_setter
	def int_size( self, n ):
		if not (0 <= n <= 128 and is_pow2(n)):
			raise ValueError(
			'int_size must be a power of 2 between 0 and 128, not {:d}'.format(n))

		self._int_size = n


	@property
	def element_count( self ):
		return self._element_count

	def set_element_count( self, n, load_factor=1 ):
		assert n >= 0
		assert load_factor > 0
		self._element_count = n

		if n > 0:
			bc1 = math.ceil(n / load_factor)
			bc2 = 1 << (bc1.bit_length() - 1)
			bc2 <<= bc1 != bc2
			self.bucket_count = bc2
		else:
			self.bucket_count = 0


	@bucket_count.setter
	def bucket_count( self, n ):
		if not (n >= 0 and is_pow2(n)):
			raise ValueError(
			'Bucket count must be a non-negative power of 2, not {:d}'.format(n))

		self._bucket_count = n
		self._bucket_mask = max(n - 1, 0)
		self._vardata = None


	def reevaluate( self ):
		self._vardata = None


	def vardata( self, force=False ):
		if force or self._vardata is None:
			if any(getattr(self, k) is None for k in self._vardata_keys):
				raise RuntimeError(
					'One or more of \'{}\' were never assigned'
						.format('\', \''.join(self._vardata_keys)))

			self._vardata = (
				util.pad_multiple_of(8,
					pickle.dumps({ k: getattr(self, k) for k in self._vardata_keys })))

		return self._vardata


	def get_bucket_idx( self, buf ):
		return self.hasher(buf, self.pickler.dump_single) & self._bucket_mask


	def value_offset( self ):
		return self.index_offset + self.bucket_count * self.int_size


	def int_to_bytes( self, n ):
		return n.to_bytes(self.int_size, self.byteorder)


	def run_estimates( self, items ):
		est = getattr(self.pickler, 'run_estimates', None)
		if est is not None: est(items)


	@classmethod
	def get_magic( cls ):
		if cls.byteorder == 'little':
			return cls._magic
		if cls.byteorder == 'big':
			return cls._magic[::-1]
		raise Exception('Unknown byte order: {!r}'.format(cls.byteorder))


	def calculate_sizes( self, buckets=None, force=False ):
		# Calculate index offset
		assert len(self._magic) % 8 == 0
		self.index_offset = (
			len(self._magic) + self._struct.size + len(self.vardata(force)))

		# Calculate int_size
		if buckets is not None:
			max_int = sum(map(len, buckets))
			self.int_size = ceil_pow2(ceil_div(max_int.bit_length(), 8))
			assert 0 <= self.int_size.bit_length() <= 8


	def to_bytes( self, buf=None, buckets=None ):
		self.calculate_sizes(buckets)

		if buf is None:
			buf = bytearray(self.index_offset)

		magic = self.get_magic()
		buf[:len(magic)] = magic
		self._struct.pack_into(buf, len(magic),
			self._version, self.int_size, self.index_offset)
		buf[len(magic) + self._struct.size:] = self.vardata()
		return buf


	def to_file( self, file, buckets=None ):
		file.write(self.to_bytes(None, buckets))
		if buckets:
			util.iter.each(file.write, map(self.int_to_bytes,
				util.iter.saccumulate(0, map(len, buckets), slice(len(buckets) - 1))))
			util.iter.each(file.write, buckets)


	@classmethod
	def from_bytes( cls, b ):
		expected_magic = cls.get_magic()
		magic = bytes(b[:len(expected_magic)])
		if magic != expected_magic:
			raise ValueError(
				'Unknown magic {!r}, expected {!r}'.format(magic, expected_magic))

		s = cls._struct.unpack_from(b, len(magic))
		assert len(s) == len(cls._struct_keys)
		s = dict(zip(cls._struct_keys, s))

		version = s.pop('version')
		if version != cls._version:
			raise ValueError(
				'Unsupported version {:d}, expected {:d}'.format(
					version, cls._version))

		var = pickle.loads(
			b[ len(magic) + cls._struct.size : s['index_offset'] ])
		missing_keys = tuple(
			itertools.filterfalse(var.__contains__, cls._vardata_keys))
		if missing_keys:
			raise ValueError('Keys missing from header: {}'
				.format(', '.join(missing_keys)))

		h = cls(var.pop('hasher'), var.pop('pickler'), s.pop('int_size'))
		h._element_count = var.pop('element_count')
		stareach(h.__setattr__, itertools.chain(s.items(), var.items()))
		return h
