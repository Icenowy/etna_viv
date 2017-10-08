#!/usr/bin/python
'''
Parse execution data log stream.
'''
# Copyright (c) 2012-2017 Wladimir J. van der Laan
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sub license,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the
# next paragraph) shall be included in all copies or substantial portions
# of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
from __future__ import print_function, division, unicode_literals
import argparse
import os, sys, struct
import json
from collections import defaultdict, namedtuple

from binascii import b2a_hex

from etnaviv.util import rnndb_path
from etnaviv.target_arch import bytes_to_words, ENDIAN, WORD_SPEC, WORD_CHAR
# Parse execution data log files
from etnaviv.parse_fdr import FDRLoader, Event, Comment
# Extract C structures from memory
from etnaviv.extract_structure import extract_structure, ResolverBase, UNRESOLVED
# Print C structures
from etnaviv.dump_structure import dump_structure, print_address
# Parse rules-ng-ng format for state space
from etnaviv.parse_rng import parse_rng_file, format_path, BitSet, Domain
from etnaviv.dump_cmdstream_util import int_as_float, fixp_as_float
from etnaviv.parse_command_buffer import parse_command_buffer,CmdStreamInfo
from etnaviv.rng_describe_c import dump_command_buffer_c
from etnaviv.auto_gcabi import guess_from_fdr
from etnaviv.counter import Counter
from etnaviv.stringutil import strip_prefix
from etnaviv.rangeutil import ranges_overlap_exclusive

DEBUG = False

# Vivante ioctls (only GCHAL_INTERFACE is actually used by the driver)
IOCTL_GCHAL_INTERFACE          = 30000
IOCTL_GCHAL_KERNEL_INTERFACE   = 30001
IOCTL_GCHAL_TERMINATE          = 30002

vivante_ioctl_data_t = struct.Struct(ENDIAN + 'QQQQ')

# HAL commands without input, can hide the input arguments structure completely
CMDS_NO_INPUT = [
'gcvHAL_GET_BASE_ADDRESS',
'gcvHAL_QUERY_CHIP_IDENTITY',
'gcvHAL_QUERY_VIDEO_MEMORY',
]
# HAL commands without output, can hide the output arguments structure completely
CMDS_NO_OUTPUT = [
'gcvHAL_COMMIT',
'gcvHAL_EVENT_COMMIT'
]

def command_to_field(enum):
    '''convert to camelcase: FOO_BAR to FooBar'''
    enum = strip_prefix(enum, 'gcvHAL_').split('_')
    field = ''.join(s[0] + s[1:].lower() for s in enum)
    if field == 'EventCommit':
        field = 'Event' # exception to the rule...
    return field

class HalResolver(ResolverBase):
    '''
    Data type resolver for HAL interface commands.
    Acts as a filter for which union/struct fields to extract and show.
    '''
    def __init__(self, dir):
        self.dir = dir
    
    def filter_fields(self, s, fields_in):
        name = s.type['name']
        if name == '_u':
            if s.parent.members['command'] is UNRESOLVED:
                return {}
            enum = s.parent.members['command'].name
            if ((self.dir == 'in' and enum in CMDS_NO_INPUT) or
                (self.dir == 'out' and enum in CMDS_NO_OUTPUT)):
                return {} # no need to show input
            return {command_to_field(enum)}
        elif name == '_gcsHAL_INTERFACE':
            # these fields contains uninitialized garbage
            fields_in.difference_update(['handle', 'pid'])
            if self.dir == 'in':
                # status is an output-only field
                fields_in.difference_update(['status'])
        elif name == '_gcsHAL_ALLOCATE_CONTIGUOUS_MEMORY':
            # see gckOS_AllocateNonPagedMemory
            if self.dir == 'in':
                # These fields are not used on input
                fields_in.difference_update(['physical','logical'])
            # All three fields are filled on output
            # bytes has the number of actually allocated bytes
        elif name == '_gcsHAL_ALLOCATE_LINEAR_VIDEO_MEMORY':
            # see gckVIDMEM_AllocateLinear
            if self.dir == 'in':
                # These fields are not used on input
                fields_in.difference_update(['node'])
            else:
                return {'node'}
        elif name == '_gcsHAL_LOCK_VIDEO_MEMORY':
            if self.dir == 'in':
                fields_in.difference_update(['address','memory','physicalAddress'])
            else:
                fields_in.difference_update(['node'])
        elif name == '_gcsHAL_USER_SIGNAL':
            if self.dir == 'out': # not used as output by any subcommand
                fields_in.difference_update(['manualReset','wait','state'])
        elif name == '_gcoCMDBUF':
            # Internally used by driver, not interesting to kernel
            fields_in.difference_update(['prev','next','patchHead','patchTail'])

        return fields_in

COMPS = 'xyzw'
def format_state(pos, value, fixp, state_map, tracking):
    try:
        path = state_map.lookup_address(pos)
        path_str = format_path(path)
    except KeyError:
        path = None
        path_str = '(UNKNOWN)'
    desc = ('  [%05X]' % pos) + ' ' + path_str 
    if fixp:
        desc += ' = %f' % fixp_as_float(value)
    else:
        # For uniforms, show float value
        if (pos >= 0x05000 and pos < 0x06000) or (pos >= 0x07000 and pos < 0x08000) or (pos >= 0x30000 and pos < 0x32000):
            num = pos & 0xFFF
            spec = 'u%i.%s' % (num//16, COMPS[(num//4)%4])
            desc += ' := %f (%s)' % (int_as_float(value), spec)
        elif path is not None:
            register = path[-1][0]
            desc += ' := '
            if isinstance(register.type, Domain):
                desc += tracking.format_addr(value)
            else:
                desc += register.describe(value)
    return desc

def dump_buf(f, name, data, tracking):
    '''Dump array of 32-bit words to disk (format will always be little-endian binary).'''
    shader_num = tracking.new_shader_id()
    filename = '%s_%i.bin' % (name, shader_num)
    with open(filename, 'wb') as g:
        for word in data:
            g.write(struct.pack('<I', word))
    f.write('/* [dumped %s to %s] */ ' % (name, filename))

def dump_shader(f, name, states, start, end, tracking):
    '''Dump binary shader code to disk'''
    dump = False

    for x in range(start, end, 4):
        if x in states:
            dump = True
            pos = x
            break

    if not dump:
        return # No shader detected
    # extract code from consecutive addresses
    code = []
    while pos < end and (pos in states):
        code.append(states[pos])
        pos += 4
    dump_buf(f, name, code, tracking)

def dump_command_buffer(f, mem, addr, end_addr, depth, state_map, cmdstream_info, tracking):
    '''
    Dump Vivante command buffer contents in human-readable
    format.
    '''
    indent = '    ' * len(depth)
    f.write('{\n')
    states = [] # list of (ptr, state_addr) tuples
    words = bytes_to_words(mem[addr:end_addr])
    size = (end_addr - addr)//4
    for rec in parse_command_buffer(words, cmdstream_info):
        hide = False
        if rec.op == 1 and rec.payload_ofs == -1:
            if options.hide_load_state:
                hide = True
        if rec.state_info is not None:
            states.append((rec.ptr, rec.state_info.pos, rec.state_info.format, rec.value))
            desc = format_state(rec.state_info.pos, rec.value, rec.state_info.format, state_map, tracking)
        else:
            desc = rec.desc

        if not hide:
            f.write(indent + '    0x%08x' % rec.value)
            if rec.ptr != (size-1):
                f.write(", /* %s */\n" % desc)
            else:
                f.write("  /* %s */\n" % desc)
    f.write(indent + '}')
    if options.list_address_states:
        # Print addresses; useful for making a re-play program
        #f.write('\n' + indent + 'GPU addresses {\n')
        uniqaddr = defaultdict(list)
        for (ptr, pos, state_format, value) in states:
            try:
                path = state_map.lookup_address(pos)
            except KeyError:
                continue
            type = path[-1][0].type
            if isinstance(type, Domain): # type Domain refers to another memory space
                #f.write(indent)
                addrname = format_addr(value)
                #f.write('    {0x%x,0x%05X}, /* %s = 0x%08x (%s) */\n' % (ptr, pos, format_path(path), value, addrname))
                uniqaddr[value].append(ptr)

        #f.write(indent + '},')
        f.write('\n' + indent + 'Grouped GPU addresses {\n')
        for (value, ptrs) in uniqaddr.iteritems():
            lvalues = ' = '.join([('cmdbuf[0x%x]' % ptr) for ptr in ptrs])
            f.write(indent + '    ' + lvalues + ' = ' + format_addr(value) + ('; /* 0x%x */' % value) + '\n')

        f.write(indent + '}')

    if options.dump_shaders:
        state_by_pos = {}
        for (ptr, pos, state_format, value) in states:
            state_by_pos[pos]=value
        # 0x04000 and 0x06000 contain shader instructions
        dump_shader(f, 'vs', state_by_pos, 0x04000, 0x05000, tracking)
        dump_shader(f, 'ps', state_by_pos, 0x06000, 0x07000, tracking)
        dump_shader(f, 'vs', state_by_pos, 0x08000, 0x09000, tracking) # gc3000
        dump_shader(f, 'ps', state_by_pos, 0x09000, 0x0A000, tracking)
        dump_shader(f, 'vs', state_by_pos, 0x0C000, 0x0D000, tracking) # gc2000
        dump_shader(f, 'ps', state_by_pos, 0x0D000, 0x0E000, tracking) # XXX this offset is probably variable (gc2000)

    if options.dump_cmdbufs:
        dump_buf(f, 'cmd', words, tracking)

    if options.output_c:
        f.write('\n')
        dump_command_buffer_c(f, parse_command_buffer(words, cmdstream_info), state_map)

def parse_arguments():
    parser = argparse.ArgumentParser(description='Parse execution data log stream.')
    parser.add_argument('input', metavar='INFILE', type=str, 
            help='FDR file')
    parser.add_argument('struct_file', metavar='STRUCTFILE', type=str, 
            help='Structures definition file', default=None, nargs='?')
    parser.add_argument('--rules-file', metavar='RULESFILE', type=str, 
            help='State map definition file (rules-ng-ng)',
            default=rnndb_path('state.xml'))
    parser.add_argument('--cmdstream-file', metavar='CMDSTREAMFILE', type=str, 
            help='Command stream definition file (rules-ng-ng)',
            default=rnndb_path('cmdstream.xml'))
    parser.add_argument('-l', '--hide-load-state', dest='hide_load_state',
            default=False, action='store_const', const=True,
            help='Hide "LOAD_STATE" entries, this can make command stream a bit easier to read')
    parser.add_argument('--show-state-map', dest='show_state_map',
            default=False, action='store_const', const=True,
            help='Expand state map from context (verbose!)')
    parser.add_argument('--show-context-buffer', dest='show_context_buffer',
            default=False, action='store_const', const=True,
            help='Expand context CPU buffer (verbose!)')
    parser.add_argument('--list-address-states', dest='list_address_states',
            default=False, action='store_const', const=True,
            help='When dumping command buffer, provide list of states that contain GPU addresses')
    parser.add_argument('--dump-shaders', dest='dump_shaders',
            default=False, action='store_const', const=True,
            help='Dump shaders to file')
    parser.add_argument('--dump-cmdbufs', dest='dump_cmdbufs',
            default=False, action='store_const', const=True,
            help='Dump command buffers to file')
    parser.add_argument('--output-c', dest='output_c',
            default=False, action='store_const', const=True,
            help='Print command buffer emission in C format')
    return parser.parse_args()        

def patch_member_pointer(struct, name, ptrtype):
    for member in struct['members']:
        if member['name'] == name:
            member['indirection'] = 1
            member['type'] = ptrtype

def load_data_definitions(struct_file):
    with open(struct_file, 'r') as f:
        d = json.load(f)
    # post-patch for missing pointers
    # these are really pointers, however the kernel interface changed them to 64-bit integers
    patch_member_pointer(d['_gcsHAL_COMMIT'], 'commandBuffer', '_gcoCMDBUF')
    for struct in d.values():
        if struct['kind'] == 'structure_type':
            patch_member_pointer(struct, 'logical', 'void')

    return d

class MemNodeInfo:
    def __init__(self, node, bytes, alignment, type, flag, pool):
        self.node = node
        self.bytes = bytes
        self.alignment = alignment
        self.type = type
        self.flag = flag
        self.pool = pool

        self.released = False
        self.locked = False
        self.cacheable = None
        self.address = None # GPU address
        self.memory = None # Logical CPU address
        self.physicalAddress = None # Physical address

    def lock(self, cacheable, address, memory, physicalAddress):
        self.locked = True
        self.cacheable = cacheable
        self.address = address
        self.memory = memory
        self.physicalAddress = physicalAddress

    def unlock(self):
        self.locked = False
        self.memory = None # CPU mem is unmapped immediately
        # Don't throw away GPU address here. See comment for
        # handle_ReleaseVideoMemory for reasoning.

    def __repr__(self):
        return 'MemNodeInfo(node=%s, bytes=%s, alignment=%s, type=%s, flag=%s, pool=%s)' % (
                repr(self.node),
                repr(self.bytes), repr(self.alignment), repr(self.type), repr(self.flag), repr(self.pool)
                )

class DriverState:
    '''
    Track current driver state.
    Might want to have driver-version specific subclasses at some point...
    '''
    def __init__(self, defs):
        self.shader_num = 0
        self.thread_id = Counter()
        self.saved_input = {} # Saved ioctl state per thread
        self.nodes = {} # Video memory nodes
        # defs is not currently used, but is passed to allow customizing
        # parsing based on driver structure definitions.

    def thread_name(self, rec):
        return '[thread %i]' % self.thread_id[rec.parameters['thread'].value]

    def meminfo_by_address(self, value):
        '''
        Look up memory info and offset by GPU address.
        '''
        for (node, meminfo) in self.nodes.items():
            if meminfo.address is None:
                continue
            if value >= meminfo.address and value < (meminfo.address + meminfo.bytes):
                return (meminfo, value - meminfo.address)
        return None

    def meminfo_collision_detection(self, spec):
        '''
        Find collisions with existing meminfo.
        '''
        collided = set()

        for (node, meminfo) in self.nodes.items():
            if meminfo is spec:
                continue
            # By GPU address
            if spec.address is not None and meminfo.address is not None:
                if ranges_overlap_exclusive((spec.address, spec.address + spec.bytes), (meminfo.address, meminfo.address + meminfo.bytes)):
                    collided.add(meminfo.node)
            # By CPU address
            if spec.memory is not None and meminfo.memory is not None:
                if ranges_overlap_exclusive((spec.memory, spec.memory + spec.bytes), (meminfo.memory, meminfo.memory + meminfo.bytes)):
                    assert(0) # This should never happen, CPU addresses are released immediately not delayed

        for dead in collided:
            # Must have been released and unlocked
            assert(self.nodes[dead].released and not self.nodes[dead].locked)
            del self.nodes[dead]

    def format_addr(self, value):
        '''
        Return description for a GPU address.
        '''
        if value == 0:
            return 'NULL'
        info = self.meminfo_by_address(value)
        if info is None:
            return '(WILD_ADDR)'
        return 'ADDR_%s_0x%x+0x%x' % (info[0].type, info[0].node, info[1])

    def new_shader_id(self):
        rv = self.shader_num
        self.shader_num += 1
        return rv

    def handle_gcin(self, thread, ifin):
        '''
        Handle ioctl input.
        '''
        assert(thread not in self.saved_input)
        self.saved_input[thread] = ifin

    def handle_gcout(self, thread, ifout):
        '''
        Handle ioctl output, and dispatch command.
        '''
        ifin = self.saved_input[thread]
        del self.saved_input[thread]
        assert(ifin.members['command'].name == ifout.members['command'].name)

        def dummy(ifin, ifout):
            pass
        field = command_to_field(ifin.members['command'].name)
        getattr(self, 'handle_' + field, dummy)(
                ifin.members['u'].members.get(field), 
                ifout.members['u'].members.get(field))

    # Handlers for specific driver commands
    def handle_AllocateLinearVideoMemory(self, gcin, gcout):
        meminfo = MemNodeInfo(
            node = gcout.members['node'].value,
            bytes = gcin.members['bytes'].value,
            alignment = gcin.members['alignment'].value,
            type = strip_prefix(gcin.members['type'].name, 'gcvSURF_'),
            flag = gcin.members['flag'].value,
            pool = strip_prefix(gcin.members['pool'].name, 'gcvPOOL_'))
        assert(meminfo.node not in self.nodes)
        self.nodes[meminfo.node] = meminfo

    def handle_WrapUserMemory(self, gcin, gcout):
        out_desc = gcout.members['desc'].members
        meminfo = MemNodeInfo(
            node = gcout.members['node'].value,
            bytes = out_desc['size'].value,
            alignment = 4096, # TODO target page size, if we care
            type = 'USER',
            flag = out_desc['flag'].value,
            pool = 'USER')
        assert(meminfo.node not in self.nodes)
        meminfo.address = meminfo.physicalAddress = out_desc['physical'].value
        meminfo.memory = out_desc['logical'].addr
        self.nodes[meminfo.node] = meminfo

        self.meminfo_collision_detection(meminfo)

    def handle_ReleaseVideoMemory(self, gcin, gcout):
        # Don't actually delete from the structure here, it seems the blob
        # does use-after-release in quite a few cases. This might be correct
        # (async - after the next event?) or not, but we want to be able to
        # print useful information nevertheless.
        # Cleanup happens in meminfo_collision_detection.
        try:
            self.nodes[gcin.members['node'].value].released = True
        except KeyError:
            return # Don't care

    def handle_LockVideoMemory(self, gcin, gcout):
        try:
            meminfo = self.nodes[gcin.members['node'].value]
        except KeyError:
            return # Don't care
        meminfo.lock(gcin.members['cacheable'].value,
                gcout.members['address'].value,
                gcout.members['memory'].value,
                gcout.members['physicalAddress'].value)

        self.meminfo_collision_detection(meminfo)

    def handle_UnlockVideoMemory(self, gcin, gcout):
        try:
            meminfo = self.nodes[gcin.members['node'].value]
        except KeyError:
            return # Don't care
        meminfo.unlock()

def main():
    args = parse_arguments()
    
    if args.struct_file is None:
        args.struct_file = guess_from_fdr(args.input)
        print('[Using struct file %s]' % args.struct_file)
    if args.struct_file is None:
        print('Could not determine struct file from GCABI in FDR, provide --struct-file= argument')

    defs = load_data_definitions(args.struct_file)
    state_xml = parse_rng_file(args.rules_file)
    state_map = state_xml.lookup_domain('VIVS')

    cmdstream_xml = parse_rng_file(args.cmdstream_file)
    fe_opcode = cmdstream_xml.lookup_type('FE_OPCODE')
    cmdstream_map = cmdstream_xml.lookup_domain('VIV_FE')
    cmdstream_info = CmdStreamInfo(fe_opcode, cmdstream_map)
    #print(fe_opcode.values_by_value[1].name)
    #print(format_path(cmdstream_map.lookup_address(0,('LOAD_STATE','FE_OPCODE'))))

    fdr = FDRLoader(args.input)
    global options
    options = args

    def handle_comment(f, val, depth):
        '''Annotate value with a comment'''
        if not depth:
            return None
        parent = depth[-1][0]
        field = depth[-1][1]
        parent_type = parent.type['name']
        # Show names for features and minor feature bits
        if parent_type == '_gcsHAL_QUERY_CHIP_IDENTITY':
            if field == 'chipMinorFeatures':
                field = 'chipMinorFeatures0'
            if field in state_xml.types and isinstance(state_xml.types[field], BitSet):
                feat = state_xml.types[field]
                active_feat = [bit.name for bit in feat.bitfields if bit.extract(val.value)]
                return ' '.join(active_feat)
        elif parent_type == '_gcsHAL_LOCK_VIDEO_MEMORY':
            if field == 'address': # annotate addresses with unique identifier
                return tracking.format_addr(val.value)

    def handle_pointer(f, ptr, depth):
        parent = depth[-1][0]
        field = depth[-1][1]
        if ptr.type in ['_gcoCMDBUF','_gcsQUEUE']:
            s = extract_structure(fdr, ptr.addr, defs, ptr.type, resolver=resolver)
            f.write('&(%s)0x%x' % (ptr.type,ptr.addr))
            dump_structure(f, s, handle_pointer, handle_comment, depth)
            return
        elif field == 'logical' and ptr.addr != 0:
            # Command stream
            if parent.type['name'] == '_gcoCMDBUF':
                f.write('&(uint32[])0x%x' % (ptr.addr))
                dump_command_buffer(f, fdr, ptr.addr + parent.members['startOffset'].value, 
                         ptr.addr + parent.members['offset'].value,
                         depth, state_map, cmdstream_info, tracking)
                return

        print_address(f, ptr, depth)

    f = sys.stdout
    tracking = DriverState(defs)

    for seq,rec in enumerate(fdr):
        if isinstance(rec, Event): # Print events as they appear in the fdr
            f.write(('[seq %i] ' % seq) + tracking.thread_name(rec) + ' ')
            params = rec.parameters
            if rec.event_type == 'MMAP_AFTER':
                f.write('mmap addr=0x%08x length=0x%08x prot=0x%08x flags=0x%08x offset=0x%08x = 0x%08x\n' % (
                    params['addr'].value, params['length'].value, params['prot'].value, params['flags'].value, params['offset'].value, 
                    params['ret'].value))
            elif rec.event_type == 'MUNMAP_AFTER':
                f.write('munmap addr=0x%08x length=0x%08x = 0x%08x\n' % (
                    params['addr'].value, params['length'].value, params['ret'].value))
            elif rec.event_type == 'IOCTL_BEFORE': # addr, length, prot, flags, offset
                if params['request'].value == IOCTL_GCHAL_INTERFACE:
                    ptr = params['ptr'].value
                    inout = vivante_ioctl_data_t.unpack(fdr[ptr:ptr+vivante_ioctl_data_t.size])
                    f.write('in=')
                    resolver = HalResolver('in')
                    s = extract_structure(fdr, inout[0], defs, '_gcsHAL_INTERFACE', resolver=resolver)
                    tracking.handle_gcin(rec.parameters['thread'].value, s)
                    dump_structure(f, s, handle_pointer, handle_comment)
                    f.write('\n')
                else:
                    f.write('Unknown Vivante ioctl %i\n' % rec.parameters['request'].value)
            elif rec.event_type == 'IOCTL_AFTER':
                if params['request'].value == IOCTL_GCHAL_INTERFACE:
                    ptr = params['ptr'].value
                    inout = vivante_ioctl_data_t.unpack(fdr[ptr:ptr+vivante_ioctl_data_t.size])
                    f.write('out=')
                    resolver = HalResolver('out')
                    s = extract_structure(fdr, inout[2], defs, '_gcsHAL_INTERFACE', resolver=resolver)
                    tracking.handle_gcout(rec.parameters['thread'].value, s)
                    dump_structure(f, s, handle_pointer, handle_comment)
                    f.write('\n')
                    f.write('/* ================================================ */\n')
                else:
                    f.write('Unknown Vivante ioctl %i\n' % rec.parameters['request'].value)
            else:
                f.write('unhandled event ' + rec.event_type + '\n')
        elif isinstance(rec, Comment):
            t = WORD_SPEC.unpack(rec.data[0:4])[0]
            if t == 0x594e4f50:
                v = struct.unpack(ENDIAN + WORD_CHAR * 3, rec.data[4:])
                print('[end of frame %d: %d.%09d]' % v)
            elif t == 0x424f4c42:
                v = struct.unpack(ENDIAN + WORD_CHAR * 4, rec.data[4:])
                print('[driver version %d.%d.%d.%d]' % v)

if __name__ == '__main__':
    main()

