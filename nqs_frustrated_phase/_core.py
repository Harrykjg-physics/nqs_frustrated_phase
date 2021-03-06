# Copyright Tom Westerhout (c) 2019
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
#     * Neither the name of Tom Westerhout nor the names of other
#       contributors may be used to endorse or promote products derived
#       from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import json
import math
import os
import pickle
import re
import sys
import time

import numba
from numba.types import uint64
import numpy as np
import scipy.sparse  # Sparse matrices
import scipy.sparse.linalg  # Diagonalisation routines
import scipy.special  # To compute binomial coefficients


@numba.jit("u8(u8, u8, u8)", nopython=True)
def _extract_part(x, i, j):
    """
    Masks out all the bits of ``x`` outside of ``[i, j)`` range. Indexing is from the
    highest significant bit to the lowest significant one.

    Example:

    >>> '{:064b}'.format(_extract_part(0xFFFFFFFFFFFFFFFF, 20, 30))
    '0000000000000000000011111111110000000000000000000000000000000000'
    """
    # Note the if else if important, because shifting by 64 results in a noop
    # rather than producing 0.
    a = (uint64(0xFFFFFFFFFFFFFFFF) >> i) if i < 64 else uint64(0)
    b = uint64(0xFFFFFFFFFFFFFFFF) << (64 - j)
    return x & a & b


@numba.jit("u8(u8)", nopython=True)
def _reverse_bits_u1(x):
    """
    Reverses the bits in a byte of data.

    The algorithm is taken from
    https://graphics.stanford.edu/~seander/bithacks.html#ReverseByteWith64Bits.
    """
    # return ((x * uint64(0x80200802)) & uint64(0x0884422110)) * uint64(0x0101010101) >> uint64(32)
    return (
        (
            ((x & 0xFF) * uint64(0x0802) & uint64(0x22110))
            | ((x & 0xFF) * uint64(0x8020) & uint64(0x88440))
        )
        * uint64(0x10101)
        >> uint64(16)
    ) & uint64(0xFF)


@numba.jit("u8(u8)", nopython=True)
def _reverse_bits_u8(x):
    """
    Reverses the bits in 8 bytes of data. We manually reverse the bytes and
    rely on ``_reverse_bits_u1`` to reverse the bits in every byte.

    **TODO**: The implementation is pretty bad, but I'm too lazy to do it
    properly (twesterhout).
    """
    return (
        (_reverse_bits_u1((x >> uint64(0)) & 0xFF) << uint64(56))
        | (_reverse_bits_u1((x >> uint64(8)) & 0xFF) << uint64(48))
        | (_reverse_bits_u1((x >> uint64(16)) & 0xFF) << uint64(40))
        | (_reverse_bits_u1((x >> uint64(24)) & 0xFF) << uint64(32))
        | (_reverse_bits_u1((x >> uint64(32)) & 0xFF) << uint64(24))
        | (_reverse_bits_u1((x >> uint64(40)) & 0xFF) << uint64(16))
        | (_reverse_bits_u1((x >> uint64(48)) & 0xFF) << uint64(8))
        | (_reverse_bits_u1((x >> uint64(56)) & 0xFF) << uint64(0))
    )


@numba.jit("u8(u8, u8, u8)", nopython=True)
def _reverse_bits_u8_part(x, i, j):
    """
    Reverses the bits of ``x[i:j]``.
    """
    a = _extract_part(x, 0, i) | _extract_part(x, j, 64)
    b = _extract_part(_reverse_bits_u8(x), 64 - j, 64 - i)
    if 64 - j > i:
        b <<= 64 - j - i
    else:
        b >>= i + j - 64
    return a | b


@numba.jit("u8(u8, u8, u8, u8)", nopython=True)
def _reverse(x, n, i, j):
    """
    64-bit integer ``x`` is treated as an array of ``n`` bits. Padding zeros
    are added to the front (i.e. where the most significant bits are). This
    function reverses the bits in ``x[i:j]``.
    """
    return _reverse_bits_u8_part(x, i + 64 - n, j + 64 - n)


@numba.jit("u8(u8, u8, u8)", nopython=True)
def _get_bit(x, n, i):
    """
    Returns the ``i``'th most significant bit of ``x``. ``n`` is the length of
    ``x``.
    """
    return (x >> (n - 1 - i)) & 1


@numba.jit("u8(u8, u8, u8, u8)", nopython=True)
def _set_bit(x, n, i, b):
    """
    Sets the ``i``'th most significant bit of ``x`` to ``b``. ``n`` is the
    length of ``x``.
    """
    return (x & ~(uint64(1) << (n - 1 - i))) | (uint64(b) << (n - 1 - i))


@numba.jit("u8(u8, u8, u8)", nopython=True)
def _flip_bit(x, n, i):
    """
    Flips the ``i``'th most significant bit of ``x``. ``n`` is the length of
    ``x``.
    """
    return x ^ (uint64(1) << (n - 1 - i))


@numba.jit("u8(u8, u8, u8, u8)", nopython=True)
def _iter_swap(x, n, i, j):
    """
    Swaps the ``i``'th and ``j``'th most significant bits of ``x``. ``n`` is
    the length of ``x``.
    """
    temp = _get_bit(x, n, i)
    x = _set_bit(x, n, i, _get_bit(x, n, j))
    x = _set_bit(x, n, j, temp)
    return x


@numba.jit("u8(u8, u8)", nopython=True)
def next_permutation(x, n):
    """
    Returns the next permutation of bits in ``x``. If there are no more
    permutations, 0 is returned.

    See https://en.cppreference.com/w/cpp/algorithm/next_permutation for an
    explanation.
    """
    if n < 2:
        return 0
    i = n - 1
    while True:
        i1 = i
        i -= 1
        if _get_bit(x, n, i) < _get_bit(x, n, i1):
            i2 = n - 1
            while not (_get_bit(x, n, i) < _get_bit(x, n, i2)):
                i2 -= 1
            x = _iter_swap(x, n, i, i2)
            x = _reverse(x, n, i1, n)
            return x
        if i == 0:
            return 0


def sector(n, m):
    """
    Iterates over all spin configurations in a sector with given magnetisation.

    :param n: Number of spins.
    :param m: Magnetisation.
    """
    assert n > 0 and abs(m) < n and (n + m) % 2 == 0
    number_ups = (n + m) // 2
    s = int("0" * (n - number_ups) + "1" * number_ups, base=2)
    while s != 0:
        yield s
        s = next_permutation(s, n)


@numba.jit("u8(u8, u8, i8[:,:], f8[:], i8[:])", nopython=True)
def _fill_row(s, n, edges, coeffs, indices) -> int:
    """
    Fills the row of the Hamiltonian matrix corresponding to the basis vector
    ``s``. ``n`` is total number of spins. ``edges`` is a 2D array of shape ?x2
    representing the adjacency list of the lattice. Column indices of non-zero
    elements are stored into ``indices`` array and the corresponding matrix
    elements -- into ``coeffs``. The total number of non-zero elements in the
    row is returned.
    """
    i = 0
    c = 0.0
    for k in range(edges.shape[0]):
        edge = edges[k]
        aligned = _get_bit(s, n, edge[0]) == _get_bit(s, n, edge[1])
        c += -1.0 + 2.0 * int(aligned)
        if not aligned:
            coeffs[i] = 2.0
            indices[i] = _flip_bit(_flip_bit(s, n, edge[0]), n, edge[1])
            i += 1
    if c != 0.0:
        coeffs[i] = c
        indices[i] = s
        i += 1
    return i


def deduce_number_of_spins(edges):
    """
    Given graph edges, tries to deduce the number of spins in the system.
    """
    smallest = min(map(min, edges))
    largest = max(map(max, edges))
    if smallest != 0:
        ValueError(
            "Failed to deduce the number of spins: counting from 0, but the minimal index present is {}.".format(
                smallest
            )
        )
    return largest + 1


class Hamiltonian(object):
    def __init__(self, n, matrix, l_to_g):
        self.n = n
        self.matrix = matrix
        # self.g_to_l = g_to_l
        self.l_to_g = l_to_g


@numba.jit("u8(u8[:,:], u8)", nopython=True)
def _binary_search(xs, y):
    first = 0
    last = len(xs)
    i = (last + first) // 2
    while True:
        if y > xs[i, 0]:
            assert last - first > 1
            first = i
            i = (last + first) // 2
        elif y < xs[i, 0]:
            assert last - first > 1
            last = i
            i = (last + first) // 2
        else:
            return xs[i, 1]


def make_hamiltonian(edges, number_of_spins=None):
    """
    Returns the Heisenberg Hamiltonian on the lattice defined by the adjacency
    list edges.
    """
    n = (
        number_of_spins
        if number_of_spins is not None
        else deduce_number_of_spins(edges)
    )
    edges = np.array(edges, dtype=np.int64).reshape(-1, 2)
    # List of all spins within a sector with a certain magnetisation
    magnetisation = n % 2
    number_ups = (n + magnetisation) // 2
    shift = number_ups * (number_ups - 1) // 2 if number_ups > 0 else 0
    print(sector(n, magnetisation))
    all_spins = np.fromiter(
        sector(n, magnetisation),
        dtype=np.uint64,
        count=int(scipy.special.comb(n, number_ups)),
    )
    print(all_spins, all_spins.shape)
    # if 2 ** n > 134217728:
    #     g_to_l = dict(((s, i) for i, s in enumerate(all_spins)))
    # else:
    #     g_to_l = np.empty(2**n, dtype=np.int64)
    #     g_to_l[:] = 2**63 - 1
    #     for i, s in enumerate(all_spins):
    #         g_to_l[s] = i
    global_to_local = np.empty((len(all_spins), 2), dtype=np.uint64)
    for i, s in enumerate(all_spins):
        global_to_local[i, 0] = s
        global_to_local[i, 1] = i

    size_step = 65536000
    size = min(size_step, len(all_spins) * (len(edges) + 1))
    data = np.empty(size, dtype=np.float64)
    row_ind = np.empty(size, dtype=np.int64)
    col_ind = np.empty(size, dtype=np.int64)
    coeffs = np.empty(len(edges) + 1, dtype=np.float64)
    indices = np.empty(len(edges) + 1, dtype=np.int64)
    count = 0
    for s in all_spins:
        k = _fill_row(s, n, edges, coeffs, indices)
        if count + k > size:
            size += size_step
            data = np.resize(data, size)
            row_ind = np.resize(row_ind, size)
            col_ind = np.resize(col_ind, size)
        data[count : count + k] = coeffs[:k]
        row_ind[count : count + k] = _binary_search(global_to_local, s)
        col_ind[count : count + k] = np.fromiter(
            (_binary_search(global_to_local, i) for i in indices[:k]),
            dtype=np.int64,
            count=k,
        )
        # row_ind[count:count + k] = g_to_l[s]
        # col_ind[count:count + k] = np.fromiter((g_to_l[i] for i in indices[:k]), dtype=np.int64, count=k)
        count += k
    data = data[:count]
    row_ind = row_ind[:count]
    col_ind = col_ind[:count]
    matrix = scipy.sparse.csr_matrix(
        (data, (row_ind, col_ind)), shape=(len(all_spins), len(all_spins))
    )
    return Hamiltonian(n, matrix, all_spins)


class System(object):
    @classmethod
    def diagonalise(cls, j2s, out_dir=None):
        number_of_spins = len(cls.POSITIONS)
        if out_dir is None:
            this_folder = os.path.dirname(os.path.realpath(__file__))
            out_dir = os.path.join(this_folder, "..", "data")
        model_folder = os.path.join(
            out_dir,
            re.match(r"^(.+[^0-9])[0-9]+$", cls.__name__).group(1).lower(),
            str(number_of_spins),
            "exact",
        )
        H_j1 = make_hamiltonian(cls.J1_EDGES, number_of_spins)
        H_j2 = make_hamiltonian(cls.J2_EDGES, number_of_spins)

        xs = np.empty((len(H_j1.l_to_g), H_j1.n), dtype=np.float32)
        for i, σ in enumerate(H_j1.l_to_g):
            spin = "{sigma:0{n}b}".format(sigma=σ, n=number_of_spins)
            for k in range(number_of_spins):
                xs[i, k] = spin[k] == "1"
        xs *= 2
        xs -= 1

        os.makedirs(model_folder, exist_ok=True)

        info = []
        for j2 in j2s:
            j2 = round(1000 * j2) / 1000
            H = H_j1.matrix + j2 * H_j2.matrix
            E, ys = scipy.sparse.linalg.eigsh(H, k=1, which="SA")
            H = None
            E = E[0]
            ys = ys.astype(np.float32)
            dataset_file = os.path.join(
                model_folder, "dataset_{:04d}.pickle".format(int(round(1000 * j2)))
            )
            with open(dataset_file, "wb") as out:
                pickle.dump((xs, ys), out)
            info.append(
                {"j2": j2, "energy": E, "dataset": os.path.realpath(dataset_file)}
            )

        if os.path.exists(os.path.join(model_folder, "info.json")):
            with open(os.path.join(model_folder, "info.json"), "r") as input:
                for _obj in json.load(input):
                    j2 = _obj["j2"]
                    if j2 not in (x["j2"] for x in info):
                        info.append(_obj)

        info = sorted(info, key=lambda x: x["j2"])
        with open(os.path.join(model_folder, "info.json"), "w") as out:
            json.dump(info, out)
