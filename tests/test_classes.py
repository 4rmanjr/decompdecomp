"""C++ classes recovered from MIPS code: a constructor that stores its base's vtable and then its own, and virtual
functions that read and write fields of `this`. Hand-assembled, so no compiler is needed."""

import struct

from decompnet import classes

T0, T1, A0, V0, V1, RA = 8, 9, 4, 2, 3, 31
LO16 = classes.R_MIPS_LO16


def i(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


def code(*words):
    return struct.pack(f"<{len(words)}I", *words)


JR_RA, NOP = 0x03E00008, 0

CTOR, BDTOR, BGET, DDTOR, DNEW = 0x100000, 0x100100, 0x100200, 0x100300, 0x100400
BASE_VT, DERIVED_VT = 0x300000, 0x300010

FUNCTIONS = [
    # D::D(): the inlined base constructor stores B's vtable, then D's own; then this->f_04 = 0.0f, this->s_22 = 0
    (CTOR, code(i(0x0F, 0, T0, 0x30), i(0x09, T0, T0, 0x0000), i(0x2B, A0, T0, 0),
                i(0x0F, 0, T1, 0x30), i(0x09, T1, T1, 0x0010), i(0x2B, A0, T1, 0),
                i(0x39, A0, 0, 4), i(0x29, A0, 0, 0x22), JR_RA, NOP),
     [[4, LO16, BASE_VT], [16, LO16, DERIVED_VT]]),
    (BDTOR, code(JR_RA, NOP), []),
    # B::get(): return this->p_14->x (the word at +0x14 is used as an address: a pointer)
    (BGET, code(i(0x23, A0, V0, 0x14), i(0x23, V0, V1, 0), JR_RA, NOP), []),
    (DDTOR, code(JR_RA, NOP), []),
    # D::extra(): reads a float at +4 and a signed short at +0x22
    (DNEW, code(i(0x31, A0, 0, 4), i(0x21, A0, V0, 0x22), JR_RA, NOP), []),
]
DATA = {BASE_VT: struct.pack("<8I", 0, 0, BDTOR, BGET, 0, 0, DDTOR, BGET) + struct.pack("<I", DNEW)}


def read(addr, n):
    for start, blob in DATA.items():
        if start <= addr < start + len(blob):
            return blob[addr - start:addr - start + n]
    return None


def test_a_derived_class_its_base_and_their_fields():
    found = classes.recover(FUNCTIONS, read, big_endian=False)
    assert set(found.by_vtable) == {BASE_VT, DERIVED_VT}
    base, derived = found.by_vtable[BASE_VT], found.by_vtable[DERIVED_VT]
    assert base.slots == [BDTOR, BGET] and derived.slots == [DDTOR, BGET, DNEW]
    # the constructor is the derived class's (it stores the base's vtable on the way), and names its base
    assert derived.ctors == {CTOR} and not base.ctors and derived.parent == BASE_VT
    assert {o: f.ctype() for o, f in derived.fields.items()} == {4: "float", 0x14: "void *", 0x22: "s16"}
    assert {o: f.ctype() for o, f in base.fields.items()} == {0x14: "void *"}
    assert found.find(BGET) == [base, derived]

    text = classes.describe(derived, found, lambda a: f"x_{a:X}", focus=DNEW)
    assert "+0x14: void *" in text and "base class x_300000" in text
    assert "1 (+0xC): x_100200, inherited" in text and "2 (+0x10): x_100400, new ← this function" in text


def test_code_without_vtables_has_no_classes():
    assert not classes.recover([(BDTOR, code(JR_RA, NOP), [])], read, big_endian=False).by_vtable
