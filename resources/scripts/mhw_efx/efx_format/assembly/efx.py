# -*- coding: utf-8 -*-
"""整个 EFX 文件的结构 ↔ 字节。

维护约束：
- 各段条目数、标签表长度、EOF 数由内容推出，不单独保存；``subselectSize`` 与
  ``doubleBuffer`` 是文件级设置，原样保存。
- 标签表按「每个标签以 null 结尾，末尾补若干 null」表示；末尾的空标签计入填充。
- Main 段整体无法解析的文件不能结构化。
"""
from __future__ import annotations

from ..efxfile import EFXFile, EFXHeader, SubselectTable
from .action import action_to_json, action_from_json
from .extern import extern_to_json, extern_from_json
from .entry import entry_to_json, entry_from_json
from .jsonval import to_json, from_json


def _labels_to_json(label_bytes: bytes):
    if label_bytes and not label_bytes.endswith(b'\x00'):
        raise ValueError('标签表未以 null 结尾')
    labels = label_bytes.split(b'\x00')[:-1] if label_bytes else []
    pad = 0
    while labels and labels[-1] == b'':
        labels.pop()
        pad += 1
    return to_json(labels), pad


def _labels_from_json(labels, pad: int) -> bytes:
    return b''.join(l + b'\x00' for l in from_json(labels)) + b'\x00' * pad


def efx_to_json(efx: EFXFile) -> dict:
    if efx.main_opaque:
        raise ValueError('Main 段无法解析，不能结构化')
    h = efx.header
    labels, pad = _labels_to_json(efx.label_bytes)
    return {
        'header': to_json({
            'signature': h.signature, 'version': h.version, 'constant': list(h.constant),
            'efxr': h.efxr, 'is3d': h.is_3d, 'unkn1': h.unkn1,
            'subselectSize': h.subselect_size, 'doubleBuffer': h.double_buffer,
        }),
        'labels': labels,
        'labelPadding': pad,
        'actions': [action_to_json(pd) for pd in efx.play],
        'externs': [extern_to_json(ea) for ea in efx.extern],
        'entries': [entry_to_json(b) for b in efx.main],
        'subselect': [{'tableType': t.table_type, 'unkn0': list(t.unkn0),
                       'entries': list(t.entries)} for t in efx.subselect],
        'eof': list(efx.eof_ints),
        'eofTail': to_json(efx.eof_tail),
    }


def efx_from_json(obj: dict) -> EFXFile:
    hd = from_json(obj['header'])
    efx = EFXFile()
    efx.label_bytes = _labels_from_json(obj['labels'], obj['labelPadding'])
    efx.labels = [s.decode('utf-8', errors='replace') for s in from_json(obj['labels']) if s]
    efx.play = [action_from_json(a) for a in obj['actions']]
    efx.extern = [extern_from_json(e) for e in obj['externs']]
    efx.main = [entry_from_json(e) for e in obj['entries']]
    efx.subselect = [SubselectTable(table_type=t['tableType'], unkn0=tuple(t['unkn0']),
                                    entries=list(t['entries'])) for t in obj['subselect']]
    efx.eof_ints = list(obj['eof'])
    efx.eof_tail = from_json(obj['eofTail'])
    efx.header = EFXHeader(
        signature=hd['signature'], version=hd['version'], constant=tuple(hd['constant']),
        efxr=hd['efxr'], is_3d=hd['is3d'], unkn1=hd['unkn1'],
        count_body=len(efx.main), label_size=len(efx.label_bytes),
        count_play=len(efx.play), count_extern=len(efx.extern),
        count_subselect=len(efx.subselect), subselect_size=hd['subselectSize'],
        count_eof=len(efx.eof_ints), double_buffer=hd['doubleBuffer'],
    )
    return efx
