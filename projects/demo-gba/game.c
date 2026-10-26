typedef unsigned int u32;

volatile u32 counter;

u32 clamp_s32(int value) {
    if (value < 0) return 0;
    if (value > 100) return 100;
    return (u32)value;
}

u32 demo_tick(u32 value) {
    counter += value;
    return clamp_s32((int)counter);
}
