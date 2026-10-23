"""External dependency edits retain paths, types, ordering and header metadata."""
import unittest

from file_handlers.rsz.rsz_file import RszFile, RSZUserDataInfo, RszInstanceInfo
from file_handlers.rsz.userdata_dependencies import dependencies, synchronize, verify


class UserdataDependencyTests(unittest.TestCase):
    def setUp(self):
        self.file = RszFile()
        self.file.instance_infos = [RszInstanceInfo()]

    def external(self, type_id, path):
        info = RszInstanceInfo()
        info.type_id = type_id
        self.file.instance_infos.append(info)
        ref = RSZUserDataInfo()
        ref.instance_id = len(self.file.instance_infos) - 1
        self.file.rsz_userdata_infos.append(ref)
        self.file.set_rsz_userdata_string(ref, path)
        return ref

    def header(self, type_id, path, crc=0):
        ref = RSZUserDataInfo()
        ref.hash, ref.crc = type_id, crc
        self.file.userdata_infos.append(ref)
        self.file.set_userdata_string(ref, path)
        return ref

    def test_deduplicate_type_and_path_preserving_reference_order_and_crc(self):
        self.external(20, 'enemy/shared.user\0')
        self.external(10, 'enemy/other.user')
        self.external(20, 'enemy/shared.user')
        self.external(30, 'enemy/shared.user')
        self.header(99, 'enemy/stale.user')
        existing = self.header(20, 'enemy/shared.user\0', crc=123)
        expected = [(20, 'enemy/shared.user'), (10, 'enemy/other.user'), (30, 'enemy/shared.user')]
        self.assertEqual(dependencies(self.file), expected)
        synchronize(self.file)
        verify(self.file)
        self.assertIs(self.file.userdata_infos[0], existing)
        self.assertEqual(existing.crc, 123)
        self.assertEqual([(r.hash, self.file._userdata_str_map[r]) for r in self.file.userdata_infos], expected)
        rows = self.file.userdata_infos[:]
        synchronize(self.file)
        self.assertEqual(self.file.userdata_infos, rows)

    def test_retarget_and_remove_drop_obsolete_header_dependencies(self):
        ref = self.external(20, 'old.user')
        synchronize(self.file)
        self.file.set_rsz_userdata_string(ref, 'new.user')
        with self.assertRaisesRegex(AssertionError, 'dependency mismatch'):
            verify(self.file)
        synchronize(self.file)
        verify(self.file)
        self.assertEqual(list(self.file._userdata_str_map.values()), ['new.user'])
        self.file.rsz_userdata_infos.clear()
        synchronize(self.file)
        verify(self.file)
        self.assertEqual(self.file.userdata_infos, [])
        self.assertEqual(self.file._userdata_str_map, {})

    def test_verification_is_read_only_and_rejects_extra_header_rows(self):
        self.external(20, 'a.user')
        synchronize(self.file)
        self.header(30, 'extra.user')
        rows = self.file.userdata_infos[:]
        strings = self.file._userdata_str_map.copy()
        with self.assertRaises(AssertionError):
            verify(self.file)
        self.assertEqual(self.file.userdata_infos, rows)
        self.assertEqual(self.file._userdata_str_map, strings)


if __name__ == '__main__':
    unittest.main()
