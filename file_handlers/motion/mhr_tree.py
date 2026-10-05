"""Rise MTRE v14 using the existing tree graph model.

v14 removes class/parameter-name pointers, adds managed class hashes, and
stores (node, bank, motion) triples. Tags/resources/variables headers are
explicitly unsupported until a native nonempty layout is validated.
"""
from dataclasses import dataclass, field
import math
import struct

from utils.hash_util import murmur3_hash_utf16le
from .binary import pad_to_alignment, align_up
from .errors import MotionParseError, MotionWriteError
from .mot_tree.model import (MotTree, TreeNode, TreeParameter, TreeParameterType,
                             TreeLink, TreeLinkType, TreeNodeType)

CLASS_HASHES = {'MotionNode': 0x92d04ffd, 'OctSyncBlendNode': 0x0be629a9,
                'VariablesNode': 0x4e9d884a, 'SyncBlendNode': 0x2aef3a2e}
BANK_HASH, MOTION_HASH = 0x5cd4677b, 0x9f595148
PARAMETER_LAYOUTS = {
    'MotionNode': ((12,0xd7cec035),(6,BANK_HASH),(6,MOTION_HASH)),
    'VariablesNode': ((12,0xd7cec035),),
    'OctSyncBlendNode': ((0,0x397965fd),(9,0x8d50d41a),(0,0x1b6709a7),(6,0xf542ce84)),
    'SyncBlendNode': ((0,0x397965fd),(9,0x8d50d41a),(0,0x1b6709a7),(6,0xf542ce84)),
}


@dataclass(slots=True)
class RiseTreeParameter(TreeParameter):
    name_hash: int = 0


@dataclass(slots=True, eq=False)
class RiseTreeNode(TreeNode):
    class_hash: int = 0
    type_name_hash: int = 0


@dataclass(slots=True)
class RiseMotionReference:
    node: TreeNode
    bank_id: int
    motion_id: int


@dataclass(slots=True)
class RiseTree(MotTree):
    motion_references: list = field(default_factory=list)
    source: bytes = b''
    original_signature: tuple = ()

    def signature(self):
        indexes = {id(n): i for i,n in enumerate(self.nodes)}
        return (self.name, tuple((n.class_name,n.name,n.authored_id,int(n.node_type),
                n.class_hash,n.type_name_hash,tuple((p.name_hash,int(p.parameter_type),p.value) for p in n.parameters))
                for n in self.nodes), indexes.get(id(self.root)),
                tuple((indexes.get(id(l.input_node)),l.input_pin,indexes.get(id(l.output_node)),l.output_pin,
                       int(l.link_type),l.output_guid) for l in self.links),
                tuple((indexes.get(id(r.node)),r.bank_id,r.motion_id) for r in self.motion_references))


def validate_tree(tree, error=MotionWriteError):
    def require(condition, message):
        if not condition: raise error(message)
    require(isinstance(tree,RiseTree), 'Expected a Rise v14 tree')
    require(isinstance(tree.name,str) and tree.name and '\0' not in tree.name,'Invalid v14 tree name')
    require(0 < len(tree.nodes) <= 65535 and len(tree.links)<=65535, 'Invalid v14 graph size')
    indexes={id(n):i for i,n in enumerate(tree.nodes)}
    require(len(indexes)==len(tree.nodes), 'Duplicate v14 node object')
    require(id(tree.root) in indexes, 'Invalid v14 root reference')
    require(len({n.authored_id for n in tree.nodes})==len(tree.nodes),'Duplicate v14 authored node identity')
    for node in tree.nodes:
        require(node.name is None or (isinstance(node.name,str) and node.name and '\0' not in node.name),'Invalid v14 node name')
        require(node.class_name in CLASS_HASHES and node.class_hash==CLASS_HASHES[node.class_name], 'Unsupported v14 node class/hash')
        require(node.node_type==(TreeNodeType.COMPONENT if node.class_name=='VariablesNode' else TreeNodeType.GAME_OBJECT),
                'Unsupported v14 class/node type pairing')
        require(not node.tags and not tree.motion_id_remaps, 'Unsupported v14 tags/legacy remaps')
        require(type(node.authored_id) is int and 0<=node.authored_id<=0xffffffff,'Invalid v14 authored node identity')
        require(node.type_name_hash==murmur3_hash_utf16le(node.class_name.removesuffix('Node')), 'Invalid v14 type-name hash')
        require(len(node.parameters)<=255, 'Too many v14 parameters')
        require(tuple((int(p.parameter_type),p.name_hash) for p in node.parameters)==PARAMETER_LAYOUTS[node.class_name],
                'Unsupported v14 class parameter layout')
        for prop in node.parameters:
            kind=prop.parameter_type
            require(kind in TreeParameterType, 'Unsupported v14 parameter type')
            if kind==TreeParameterType.BOOL:
                require(type(prop.value) is bool,'v14 BOOL requires bool')
            elif kind==TreeParameterType.U32:
                require(type(prop.value) is int and 0<=prop.value<=0xffffffff,'Invalid v14 U32')
            elif kind==TreeParameterType.F32:
                require(isinstance(prop.value,(float,int)) and math.isfinite(prop.value),'Invalid v14 F32')
            else:
                require(isinstance(prop.value,str),'Invalid v14 STR16')
    for link in tree.links:
        require(id(link.input_node) in indexes and id(link.output_node) in indexes, 'Dangling v14 link')
        require(link.link_type in (TreeLinkType.MOTION,TreeLinkType.PARAMETER), 'Unsupported v14 link type')
        require(link.output_guid is None or len(link.output_guid)==16,'v14 GUID must contain 16 bytes')
        if link.link_type==TreeLinkType.PARAMETER:
            require(link.output_node.class_name=='VariablesNode' and link.output_guid is not None,
                    'v14 parameter link requires a VariablesNode and GUID')
    visiting,visited=set(),set()
    def visit(node):
        key=id(node)
        require(key not in visiting,'Cyclic v14 graph')
        if key in visited:return
        visiting.add(key)
        for link in tree.links:
            if link.input_node is node:visit(link.output_node)
        visiting.remove(key);visited.add(key)
    for node in tree.nodes:visit(node)
    targets=[]
    for ref in tree.motion_references:
        require(id(ref.node) in indexes and ref.node.class_name=='MotionNode','Invalid v14 leaf reference')
        require(0<=ref.bank_id<=65535 and 0<=ref.motion_id<=65535,'Invalid v14 bank/motion identity')
        targets.append(id(ref.node))
        params={p.name_hash:p for p in ref.node.parameters}
        require(BANK_HASH in params and MOTION_HASH in params,'v14 leaf lacks BankID/MotionID')
        require(params[BANK_HASH].value==ref.bank_id and params[MOTION_HASH].value==ref.motion_id,
                'v14 leaf parameters disagree with motion reference table')
    require(len(targets)==len(set(targets)), 'Duplicate v14 leaf reference')
    require(set(targets)=={id(n) for n in tree.nodes if n.class_name=='MotionNode'}, 'Missing v14 leaf reference')


def parse_tree(parser,c,base,end,group):
    covered=bytearray(end-base)
    def cover(start,size):
        c.require(start,size,'v14 owned region')
        covered[start-base:start-base+size]=b'\1'*size
    c.require(base,80,'MTRE v14 header')
    cover(base,80)
    if c.u32(base)!=14 or c.u64(base+8) or c.u64(base+16) or c.u64(base+24):
        raise MotionParseError(f'{c.label}: unsupported MTRE header/resources/variables')
    if any(c.u16(base+p) for p in (70,74,76,78)):
        raise MotionParseError(f'{c.label}: unsupported MTRE header counts')
    nodes,links,name,refs=[parser.pointer(c,group,n,base+o,base) for n,o in
                         (('Tree nodes',32),('Tree links',40),('Tree name',48),('Tree leaf refs',56))]
    nc,lc,root,rc=c.u16(base+64),c.u16(base+66),c.u16(base+68),c.u16(base+72)
    c.require(nodes,nc*56,'v14 node records');c.require(links,lc*40,'v14 link records');c.require(refs,rc*6,'v14 leaf refs')
    cover(nodes,nc*56);cover(links,lc*40)
    if rc:cover(refs,rc*6)
    _,name_end=c.utf16_z(name);cover(name,name_end-name)
    tree=RiseTree(c.utf16_z(name)[0],source=c.bytes(base,end-base))
    class_cursor=nodes+nc*56
    for i in range(nc):
        r=nodes+i*56
        class_start=class_cursor
        cls,class_cursor=c.ascii_z(class_cursor,'v14 class name')
        cover(class_start,class_cursor-class_start)
        if c.u64(r+8) or c.u64(r+16) or c.u8(r+48) or c.u8(r+51) or c.u32(r+52):
            raise MotionParseError(f'{c.label}: unsupported v14 node tags/flags')
        ptr=parser.pointer(c,group,'Node name',r,base)
        pn=parser.pointer(c,group,'Node parameters',r+24,base)
        node_name,name_end=c.utf16_z(ptr);cover(ptr,name_end-ptr)
        if c.u32(r+44) != (murmur3_hash_utf16le(node_name) if node_name else 0):
            raise MotionParseError(f'{c.label}: invalid v14 node name hash')
        node=RiseTreeNode(cls,node_name or None,c.u32(r+32),TreeNodeType(c.u8(r+50)),
                         class_hash=c.u32(r+36),type_name_hash=c.u32(r+40))
        tree.nodes.append(node)
        pc=c.u8(r+49);c.require(pn,pc*16,'v14 parameters')
        if pc:cover(pn,pc*16)
        for j in range(pc):
            p=pn+j*16
            try: kind=TreeParameterType(c.u32(p))
            except ValueError as exc: raise MotionParseError(f'{c.label}: unsupported v14 parameter') from exc
            raw=c.u64(p+8)
            if kind==TreeParameterType.STR16:
                parser.relocation(c,p+8,base);value,string_end=c.utf16_z(base+raw)
                cover(base+raw,string_end-base-raw)
            elif kind==TreeParameterType.F32:
                if raw>>32: raise MotionParseError(f'{c.label}: v14 F32 padding')
                value=c.f32(p+8)
            elif kind==TreeParameterType.BOOL:
                if raw not in (0,1): raise MotionParseError(f'{c.label}: invalid v14 BOOL')
                value=bool(raw)
            else:
                if raw>>32: raise MotionParseError(f'{c.label}: v14 U32 padding')
                value=raw
            h=c.u32(p+4)
            node.parameters.append(RiseTreeParameter({BANK_HASH:'BankID',MOTION_HASH:'MotionID'}.get(h,f'Hash_{h:08X}'),kind,value,h))
    if root>=nc: raise MotionParseError(f'{c.label}: invalid v14 tree root')
    tree.root=tree.nodes[root]
    for i in range(lc):
        r=links+i*40
        inp,ip,out,op,kind,reserved,ig,og=struct.unpack_from('<6I2Q',c.data,r)
        if inp>=nc or out>=nc or reserved or ig or kind not in (1,2):
            raise MotionParseError(f'{c.label}: invalid/unsupported v14 link')
        if og: parser.relocation(c,r+32,base)
        if og:cover(base+og,16)
        tree.links.append(TreeLink(tree.nodes[inp],ip,tree.nodes[out],op,TreeLinkType(kind),c.bytes(base+og,16) if og else None))
    for i in range(rc):
        idx,bank,mot=struct.unpack_from('<3H',c.data,refs+i*6)
        if idx>=nc: raise MotionParseError(f'{c.label}: invalid v14 leaf index')
        tree.motion_references.append(RiseMotionReference(tree.nodes[idx],bank,mot))
    validate_tree(tree,MotionParseError)
    if any(byte and not covered[i] for i,byte in enumerate(c.bytes(base,end-base))):
        raise MotionParseError(f'{c.label}: unsupported nonzero v14 data outside identified records')
    tree.original_signature=tree.signature()
    parser.model.tree_spans.append((base,end,tree))
    return tree


def build_tree(tree):
    validate_tree(tree)
    if tree.source and tree.signature()==tree.original_signature:
        return tree.source
    out=bytearray(80)
    def text(value,wide=True):
        if wide: pad_to_alignment(out,2)
        pos=len(out);out.extend(value.encode('utf-16le' if wide else 'ascii')+(b'\0\0' if wide else b'\0'));return pos
    idx={id(n):i for i,n in enumerate(tree.nodes)}
    ref_at=len(out) if tree.motion_references else 0
    for ref in tree.motion_references:out.extend(struct.pack('<3H',idx[id(ref.node)],ref.bank_id,ref.motion_id))
    pad_to_alignment(out,8);name=text(tree.name);pad_to_alignment(out,8)
    nodes=len(out);out.extend(bytes(len(tree.nodes)*56))
    for node in tree.nodes:text(node.class_name,False)
    names=[text(n.name or '') for n in tree.nodes]
    pad_to_alignment(out,8)
    params=[]
    for node in tree.nodes:
        pos=len(out) if node.parameters else 0;params.append(pos)
        out.extend(bytes(len(node.parameters)*16))
    for node,pos in zip(tree.nodes,params):
        for j,p in enumerate(node.parameters):
            r=pos+j*16;struct.pack_into('<II',out,r,int(p.parameter_type),p.name_hash)
            if p.parameter_type==TreeParameterType.STR16:struct.pack_into('<Q',out,r+8,text(p.value))
            elif p.parameter_type==TreeParameterType.F32:struct.pack_into('<f',out,r+8,p.value)
            else:struct.pack_into('<Q',out,r+8,int(p.value))
    pad_to_alignment(out,16);links=len(out)
    for l in tree.links:out.extend(struct.pack('<6I2Q',idx[id(l.input_node)],l.input_pin,idx[id(l.output_node)],l.output_pin,int(l.link_type),0,0,0))
    pad_to_alignment(out,16)
    for i,l in enumerate(tree.links):
        if l.output_guid is not None:
            struct.pack_into('<Q',out,links+i*40+32,len(out));out.extend(l.output_guid)
    pad_to_alignment(out,16)
    for i,n in enumerate(tree.nodes):
        struct.pack_into('<4Q4I4BI',out,nodes+i*56,names[i],0,0,params[i],n.authored_id,n.class_hash,n.type_name_hash,
                         murmur3_hash_utf16le(n.name) if n.name else 0,0,len(n.parameters),int(n.node_type),0,0)
    struct.pack_into('<I4s',out,0,14,b'mtre')
    struct.pack_into('<4Q8H',out,32,nodes,links,name,ref_at,len(tree.nodes),len(tree.links),idx[id(tree.root)],0,len(tree.motion_references),0,0,0)
    return bytes(out)


def set_leaf(tree,node,bank_id,motion_id):
    matches=[r for r in tree.motion_references if r.node is node]
    if len(matches)!=1:raise MotionWriteError('Select one v14 leaf reference')
    matches[0].bank_id,matches[0].motion_id=bank_id,motion_id
    params={p.name_hash:p for p in node.parameters}
    params[BANK_HASH].value,params[MOTION_HASH].value=bank_id,motion_id
