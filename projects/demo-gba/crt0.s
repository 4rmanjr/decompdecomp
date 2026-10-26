    .section .text.start, "ax"
    .arm
    .global _start
_start:
    b demo_tick
    .fill 156, 1, 0
    .ascii "DEMO QUEST  "
    .ascii "DQAE"
    .ascii "00"
    .byte 0x96, 0, 0
    .fill 7, 1, 0
    .byte 0, 0, 0
