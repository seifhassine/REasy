"""Explicit header dependency maintenance for external USER/PFB/SCN userdata.

Call after adding, removing or retargeting external RSZ references. This is not
part of an ordinary round trip: header-only dependencies may be intentional.
Embedded userdata formats do not use this external dependency contract.
"""
from .rsz_file import RSZUserDataInfo


def dependencies(file):
    """Return unique (type ID, resource path) pairs in RSZ reference order."""
    return list(dict.fromkeys(
        (file.instance_infos[ref.instance_id].type_id,
         file.get_rsz_userdata_string(ref).rstrip('\0'))
        for ref in file.rsz_userdata_infos))


def synchronize(file):
    """Replace the header dependency list, preserving surviving row metadata."""
    expected = dependencies(file)
    previous = {(ref.hash, file._userdata_str_map[ref].rstrip('\0')): ref
                for ref in file.userdata_infos}
    rows, strings = [], {}
    for key in expected:
        ref = previous.get(key)
        if ref is None:
            ref = RSZUserDataInfo()
            ref.hash = key[0]
        rows.append(ref)
        strings[ref] = key[1]
    file.userdata_infos = rows
    file._userdata_str_map = strings


def verify(file):
    """Require the synchronized dependency contract without changing the file."""
    actual = [(ref.hash, file._userdata_str_map[ref].rstrip('\0'))
              for ref in file.userdata_infos]
    assert actual == dependencies(file), 'Header/RSZ userdata dependency mismatch'
