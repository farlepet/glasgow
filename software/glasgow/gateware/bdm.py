# BDM: Background Debug Mode interface

from amaranth import *
from amaranth.lib import io
from amaranth.lib.cdc import FFSynchronizer

import math


__all__ = ["BDM"]


class BDMBus(Elaboratable):
    """
    BDM bus.

    Provides synchronization.
    """
    def __init__(self, ports):
        self.ports = ports

        self.has_reset = False
        if hasattr(ports, "reset"):
            if ports.reset is not None:
                self.has_reset = True
                self.reset   = Signal()

        self.bkgd_o   = Signal()
        self.bkgd_oen = Signal()
        self.bkgd_i   = Signal()


    def elaborate(self, platform):
        m = Module()

        m.submodules.io_bkgd = bkgd_t = io.Buffer("io", self.ports.bkgd)
        bkgd_r = Signal()

        m.d.comb += [
            bkgd_t.o.eq(self.bkgd_o),
            bkgd_t.oe.eq(self.bkgd_oen),
        ]
        m.d.sync += [
            bkgd_r.eq(self.bkgd_i),
        ]

        m.submodules += [
            FFSynchronizer(bkgd_t.i, self.bkgd_i)
        ]

        if self.has_reset:
            m.submodules.io_reset = reset_t = io.Buffer("io", self.ports.reset)
            m.d.comb += [
                # Reset is active-low
                reset_t.o.eq(0),
                reset_t.oe.eq(self.reset)
            ]

        return m


CLOCKS_FOR_0        = 14
CLOCKS_FOR_1        =  4
CLOCKS_PER_BIT      = 18
CLOCKS_UNTIL_SAMPLE = 10
CLOCKS_FOR_READ     =  4

# Command format
#  1 byte:  Flags
#    0x01 - Delay 16 cycles between write and read
#  1 byte:  Number of bytes to write
#  n bytes: Bytes to write
#  1 byte:  Number of bytes to read

# For SYNC:
#  - BKGD must be low for >= 128 BDC clock cycles
#  - BDC clock is typically >= reference osc / 64
#     - On this chip, reference osc is 8 MHz
#     - 8 MHz / 64 = 125 kHz
#     - 128 periods of 125 kHz = 1.024 ms

class BDM(Elaboratable):
    """

    """
    def __init__(self, ports, out_fifo, in_fifo, max_cycles, cycles_per_clock=None):
        self.out_fifo = out_fifo
        self.in_fifo = in_fifo

        self.max_cycles = max_cycles
        self.sync_dur = max_cycles * 128
        print("max_cycles: ", self.max_cycles, ", sync_dur: ", self.sync_dur, ", cycles_per_clock: ", cycles_per_clock)

        if cycles_per_clock is None:
            cycles_per_clock = 0
        self.cycles_per_clock = Signal(range(self.max_cycles * 2), init=cycles_per_clock)

        self.bus = BDMBus(ports)

    def elaborate(self, platform):
        m = Module()

        m.submodules.bus = self.bus

        shreg = Signal(8)
        # Wastes a bit, can be done better
        bit_no = Signal(range(9))
        bit = Signal()
        clock = Signal(range(32))
        count = Signal(range(self.sync_dur * 2))
        raw_count = Signal(range(self.sync_dur * 2))

        delay_before_read = Signal()
        n_write_bytes = Signal(3)
        n_read_bytes = Signal(3)

        m.d.sync += [
            count.eq(count + 1),
            raw_count.eq(raw_count + 1),
        ]

        with m.If(self.cycles_per_clock != 0):
            with m.If(count == (self.cycles_per_clock - 1)):
                m.d.sync += [
                    count.eq(0),
                    clock.eq(clock + 1),
                ]

        with m.FSM():
            with m.State("IDLE"):
                m.d.sync += [
                    count.eq(0),
                    raw_count.eq(0),
                    self.bus.bkgd_oen.eq(1),
                ]
                if self.bus.has_reset:
                    m.next = "RESET"
                else:
                    with m.If(self.cycles_per_clock != 0):
                        m.next = "COMMAND_FLAGS"
                    with m.Else():
                        m.next = "SYNC"

            if self.bus.has_reset:
                with m.State("RESET"):
                    with m.If(raw_count > math.floor(self.sync_dur * 1.5)):
                        m.d.sync += [
                            count.eq(0),
                            self.bus.bkgd_oen.eq(1),
                        ]
                        with m.If(self.cycles_per_clock != 0):
                            m.next = "COMMAND_FLAGS"
                        with m.Else():
                            m.next = "SYNC"
                    with m.Elif(raw_count > math.floor(self.sync_dur * 1.2)):
                        m.d.sync += [
                            self.bus.bkgd_oen.eq(0)
                        ]
                    with m.Elif(raw_count > self.sync_dur):
                        m.d.sync += [
                            self.bus.reset.eq(0),
                        ]
                    with m.Else():
                        m.d.sync += [
                            self.bus.bkgd_oen.eq(1),
                            self.bus.bkgd_o.eq(0),
                            self.bus.reset.eq(1),
                        ]

            with m.State("SYNC"):
                m.d.sync += [
                    self.bus.bkgd_o.eq(0),
                ]
                with m.If(count > self.sync_dur):
                    m.d.sync += [
                        self.bus.bkgd_o.eq(1),
                    ]
                with m.If(count > (self.sync_dur + 2)):
                    m.d.sync += [
                        self.bus.bkgd_oen.eq(0),
                        count.eq(0),
                    ]
                    m.next = "SYNC_WAIT_LOW"
            with m.State("SYNC_WAIT_LOW"):
                with m.If(~self.bus.bkgd_i):
                    m.d.sync += [
                        count.eq(0),
                    ]
                    m.next = "SYNC_WAIT_HIGH"
                with m.Elif(count > self.max_cycles):
                    m.next = "ERROR"
            with m.State("SYNC_WAIT_HIGH"):
                with m.If(self.bus.bkgd_i):
                    m.d.sync += [
                        self.cycles_per_clock.eq(count >> 7),
                        count.eq(0),
                        clock.eq(0),
                    ]
                    m.next = "SYNC_WAIT_POST"
                with m.Elif(count > self.max_cycles):
                    m.next = "ERROR"
            with m.State("SYNC_WAIT_POST"):
                # TODO: This may not be required
                with m.If(clock > 8):
                    m.next = "COMMAND_FLAGS"

            with m.State("COMMAND_FLAGS"):
                with m.If(self.out_fifo.r_rdy):
                    m.d.comb += self.out_fifo.r_en.eq(1)
                    m.d.sync += [
                        delay_before_read.eq(self.out_fifo.r_data)
                    ]
                    m.next = "COMMAND_N_WR_BYTES"
            with m.State("COMMAND_N_WR_BYTES"):
                with m.If(self.out_fifo.r_rdy):
                    m.d.comb += self.out_fifo.r_en.eq(1)
                    m.d.sync += [
                        n_write_bytes.eq(self.out_fifo.r_data)
                    ]
                    m.next = "COMMAND_WR_BYTES"
            with m.State("COMMAND_WR_BYTES"):
                with m.If(n_write_bytes == 0):
                    with m.If(delay_before_read):
                        m.d.sync += clock.eq(0)
                        m.next = "COMMAND_RD_DELAY"
                    with m.Else():
                        m.next = "COMMAND_N_RD_BYTES"
                with m.Else():
                    with m.If(self.out_fifo.r_rdy):
                        m.d.comb += self.out_fifo.r_en.eq(1)
                        m.d.sync += [
                            shreg.eq(self.out_fifo.r_data),
                            n_write_bytes.eq(n_write_bytes - 1),
                        ]
                        m.next = "WRITE_BYTE"


            with m.State("COMMAND_RD_DELAY"):
                with m.If(clock > CLOCKS_PER_BIT):
                    m.next = "COMMAND_N_RD_BYTES"

            with m.State("COMMAND_N_RD_BYTES"):
                with m.If(self.out_fifo.r_rdy):
                    m.d.comb += self.out_fifo.r_en.eq(1)
                    m.d.sync += [
                        n_read_bytes.eq(self.out_fifo.r_data)
                    ]
                    m.next = "COMMAND_RD_BYTES"
            with m.State("COMMAND_RD_BYTES"):
                with m.If(n_read_bytes == 0):
                    m.next = "COMMAND_FLAGS"
                with m.Else():
                    m.d.sync += [
                        n_read_bytes.eq(n_read_bytes - 1),
                    ]
                    m.next = "READ_BYTE"
            with m.State("COMMAND_RD_BYTE_SEND"):
                with m.If(self.in_fifo.w_rdy):
                    m.d.comb += [
                        self.in_fifo.w_data.eq(shreg),
                        self.in_fifo.w_en.eq(1),
                    ]
                    m.next = "COMMAND_RD_BYTES"


            with m.State("WRITE_BYTE"):
                with m.If(bit_no == 8):
                    # TODO
                    #m.next = "OK"
                    m.d.sync += [
                        shreg.eq(0),
                        bit_no.eq(0),
                    ]
                    m.next = "COMMAND_WR_BYTES"
                with m.Else():
                    m.d.sync += [
                        clock.eq(0),
                        bit_no.eq(bit_no + 1),
                        bit.eq(shreg[7]),
                        shreg.eq(Cat(C(0, 1), shreg[0:7])),
                    ]
                    m.next = "WRITE_BIT"
            with m.State("WRITE_BIT"):
                m.d.sync += [ self.bus.bkgd_oen.eq(1) ]

                with m.If(clock > CLOCKS_PER_BIT):
                    m.d.sync += [ self.bus.bkgd_oen.eq(0) ]
                    m.next = "WRITE_BYTE"
                with m.Elif(bit):
                    with m.If(clock > CLOCKS_FOR_1):
                        m.d.sync += [ self.bus.bkgd_o.eq(1) ]
                    with m.Else():
                        m.d.sync += [ self.bus.bkgd_o.eq(0) ]
                with m.Else():
                    with m.If(clock > CLOCKS_FOR_0):
                        m.d.sync += [ self.bus.bkgd_o.eq(1) ]
                    with m.Else():
                        m.d.sync += [ self.bus.bkgd_o.eq(0) ]


            with m.State("READ_BYTE"):
                with m.If(bit_no == 8):
                    m.next = "COMMAND_RD_BYTE_SEND"
                with m.Else():
                    m.d.sync += [
                        clock.eq(0),
                        bit_no.eq(bit_no + 1),
                    ]
                    m.next = "READ_BIT"
                pass
            with m.State("READ_BIT"):
                with m.If(clock < CLOCKS_FOR_READ):
                    m.d.sync += [
                        self.bus.bkgd_oen.eq(1),
                        self.bus.bkgd_o.eq(0),
                    ]
                with m.Else():
                    m.d.sync += [
                        self.bus.bkgd_oen.eq(0),
                    ]
                    with m.If(clock == CLOCKS_UNTIL_SAMPLE):
                        m.d.sync += [
                            shreg.eq(Cat(shreg[1:8], self.bus.bkgd_i))
                        ]
                    with m.Elif(clock > CLOCKS_PER_BIT):
                        m.next = "READ_BYTE"

            with m.State("DONE"):
                pass
            with m.State("ERROR"):
                with m.If(self.in_fifo.w_rdy):
                    m.d.comb += [
                        self.in_fifo.w_data.eq(255),
                        self.in_fifo.w_en.eq(1),
                    ]
                    m.next = "DONE"

        return m
