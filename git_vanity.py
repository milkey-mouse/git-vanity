#!/usr/bin/python2
"""
git-vanity, a script to make vanity commits by changing the committer name
Copyright (C) 2014  Tocho Tochev <tocho AT tochev DOT net>

Please tweak GS and WS to suit your video card.


    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.


TODO:
    - better probability-based counter
    - add quiet mode
    - use logging
    - timer (estimated time), MH/s stats
    - add more error handling
    - optimize GS and WS
    - support other revisions than the HEAD
    - add more documentation (as usual)
    - add option for length of name addition
    - python3 ?
    - !!! rewrite in C :)
"""

import argparse
import numpy as np
import os
import pyopencl as cl
import re
import sha
import struct
import subprocess

GS = 4*1024*1024      # GPU iteration global_size
WS = 256              # work_size


def hex2target(hex_prefix):
    """Returns 5*int32 0-padded target and bit length based on hex prefix"""
    data = hex_prefix + ('0' * (40 - len(hex_prefix)))
    target = np.array(
        [int(data[i*8 : (i+1)*8], 16) for i in range(5)],
        dtype=np.uint32)
    return target, len(hex_prefix)*4

def xrange_custom(start, stop, step):
    # deals with uint64
    current = start
    while current < stop:
        yield current
        current += step

def get_padded_size(size):
    """Returns the size of the text of size `size' after preprocessing"""
    if (size % 64) > 55:
        return ((size // 64) + 2) * 64
    return ((size // 64) + 1) * 64

def sha1_preprocess_data(data):
    size = get_padded_size(len(data))
    preprocessed_message = np.zeros(size, dtype=np.ubyte)
    preprocessed_message[:len(data)] = map(ord, data)
    preprocessed_message[len(data)] = 0x80
    preprocessed_message[-8:] = map(ord, struct.pack('>Q', len(data)*8))
    return preprocessed_message

def load_opencl():
    """Returns opencl context, queue, program"""
    CL_PROGRAM = open(
        os.path.join(
            os.path.dirname(
                os.path.realpath(
                    __file__)),
            "sha1_prefix_search.cl"),
        "rb").read()
    ctx = cl.create_some_context()
    queue = cl.CommandQueue(ctx)
    prg = cl.Program(ctx, CL_PROGRAM).build()
    return ctx, queue, prg

def extract_commit(rev):
    return subprocess.check_output(["git", "cat-file", "-p", rev])

def preprocess_commit(commit):
    """
    Returns:
        [commit_with_header_and_placeholder,
        placeholder_offset,
        committer_name, committer_mail, committer_date]
    """
    commit_lines = list(commit.splitlines())

    committer_index = commit_lines.index("") - 1
    committer_line = commit_lines[committer_index]

    match = re.match(r'committer (?P<name>.*?)'
                     r'(?P<hex> [0-9A-F]{16})? <(?P<mail>.*)> '
                     r'(?P<date>.*)',
                     committer_line)

    assert match, "Unable to parse committer line `%s'" % committer_line

    committer_name = match.group('name')
    committer_mail = match.group('mail')
    committer_date = match.group('date')
    # discard match.group('hex'), assume nobody has 64bit hex last name

    prefix = ('\n'.join(commit_lines[:committer_index]) +
              '\ncommitter ' + committer_name + ' ')
    rest = (('F'*16) + " <" + committer_mail + "> " + committer_date +
            '\n' + '\n'.join(commit_lines[committer_index + 1:]) + '\n')

    header = 'commit %d\x00' % (len(prefix) + len(rest))

    return (header + prefix + rest,
            len(header) + len(prefix),
            committer_name,
            committer_mail,
            committer_date)

def commit_add_header(commit):
    return 'commit %d\x00%s' % (len(commit), commit)

def commit_without_header(commit):
    null_index = commit.find('\x00')
    if null_index == -1:
        return commit
    return commit[null_index + 1:]

def sha1_prefix_search_opencl(data, hex_prefix, offset,
                              start=0, stop=(1 << 64),
                              opencl_vars=None,
                              gs=GS, ws=WS,
                              quiet=False):
    """Return %016x.upper() or raises a ValueError if nothing is found"""
    if opencl_vars is None:
        opencl_vars = load_opencl()
        ctx, queue, prg = opencl_vars

    target, precision_bits = hex2target(hex_prefix)
    preprocessed_message = sha1_preprocess_data(data)

    result = np.zeros(3, dtype=np.uint64)

    mf = cl.mem_flags
    # create buffers
    message_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                            hostbuf=preprocessed_message)
    target_buf = cl.Buffer(ctx, mf.READ_ONLY | mf.COPY_HOST_PTR,
                           hostbuf=target)
    result_buf = cl.Buffer(ctx, mf.WRITE_ONLY | mf.COPY_HOST_PTR,
                           hostbuf=result)

    # the main stuff
    for current_start in xrange_custom(start, stop, gs):
        #import time
        #t1 = time.time()

        if not quiet:
            print("Processing GS iteration %s" %
                  ((current_start - start) / gs + 1))
            print("Estimated remaining (randomness involved) %.6lf%% ..." %
                  (100*(1 - float(current_start - start) / (1 << precision_bits))))

        prg.sha1_prefix_search(queue,
                               (gs,),
                               (ws,),
                               message_buf,
                               struct.pack('I', preprocessed_message.shape[0]),
                               target_buf,
                               struct.pack('I', precision_bits),
                               struct.pack('I', offset),
                               struct.pack('Q', current_start),
                               result_buf)

        cl.enqueue_copy(queue, result, result_buf)

        if result[0]: # we found it
            return ('%016x' % result[1]).upper()

        #print (gs/(time.time()-t1))/(10**6), "MH/s"

    else:
        raise ValueError("Unable to find matching prefix...")

def amend_commit(committer_name,
                 committer_mail,
                 committer_date,
                 hex_magic):
    env = os.environ.copy()
    env['GIT_COMMITTER_NAME'] = committer_name + " " + hex_magic
    env['GIT_COMMITTER_EMAIL'] = committer_mail
    env['GIT_COMMITTER_DATE'] = committer_date

    print(env['GIT_COMMITTER_NAME'])

    subprocess.check_call(['git', 'commit', '--amend', '--no-edit',
                           '-c', 'HEAD'], env=env)
    subprocess.check_call(['git', 'show-ref', '--head'])

def main(hex_prefix, start=0, gs=GS, ws=WS, write_changes=False, quiet=False):
    """
    Attempts to change in the current directory
    hex_prefix is the desired prefix
    start is int or hex string
    """
    commit = extract_commit('HEAD')
    [data,
     placeholder_offset,
     committer_name,
     committer_mail,
     committer_date] = preprocess_commit(commit)

    if isinstance(start, str):
        start = int(start, 16)

    print(("Attempting to find sha1 prefix `%s'\n"
           "for commit `%s'\n"
           "================\n%s================\n\n")
          % (hex_prefix,
             sha.sha(commit_add_header(commit)).hexdigest(),
             commit))

    result = sha1_prefix_search_opencl(data,
                                       hex_prefix,
                                       placeholder_offset,
                                       start,
                                       gs=gs, ws=ws,
                                       quiet=quiet)

    final = (data[:placeholder_offset] +
             result +
             data[placeholder_offset + 16:])

    print(("\nFound sha1 prefix `%s'\n"
           "with sha1 `%s'\n"
           "Using %s\n"
           "================\n%s================\n\n")
          % (hex_prefix,
             sha.sha(final).hexdigest(),
             result,
             commit_without_header(final)))

    if write_changes:
        print("Writing changes to the repository...\n")
        amend_commit(committer_name, committer_mail, committer_date, result)
        print("All done.")
    else:
        print("Changes not written to the repository.")


if __name__ ==  '__main__':
    parser = argparse.ArgumentParser(
        description="Create vanity commit checksums"
        "by extending the committer name.")
    parser.add_argument('hex_prefix',
                        type=str,
                        help="the desired hex prefix")
    parser.add_argument('-s', '--start',
                        default='0',
                        type=lambda x: int(x, 16),
                        help="starting the search from number (hex)")
    parser.add_argument('-g', '--global-size',
                        dest='gs',
                        default=GS,
                        type=int,
                        help="OpenCL global size (careful)")
    parser.add_argument('-w', '--work-size',
                        dest='ws',
                        default=WS,
                        type=int,
                        help="OpenCL work size (64,128,256,...)")
    parser.add_argument('-W', '--write',
                        action='store_true',
                        default=False,
                        help="Enable writing to the repo")
    parser.add_argument('-q', '--quiet',
                        action='store_true',
                        default=False,
                        help="quiet mode, disables progress")

    args = parser.parse_args()

    main(args.hex_prefix, args.start,
         args.gs, args.ws,
         args.write,
         args.quiet)