ENTRY(demo_tick)
SECTIONS
{
    . = 0x08000000;
    .text : { *(.text*) *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }
    /DISCARD/ : { *(.comment) *(.ARM.attributes) *(.note*) }
}
