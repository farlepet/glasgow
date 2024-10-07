import os
import sys
import logging
import asyncio
import argparse
import math
from amaranth import *
from amaranth.lib import io

from ....support.endpoint import *
from ....gateware.bdm import *
from ... import *


BDM_COMMAND_BACKGROUND    = 0x90
BDM_COMMAND_READ_STATUS   = 0xE4
BDM_COMMAND_WRITE_CONTROL = 0xC4
BDM_COMMAND_READ_BYTE     = 0xE0
BDM_COMMAND_WRITE_BYTE    = 0xC0


class BDMInterface:
    def __init__(self, interface, logger):
        self.lower = interface
        self._logger = logger
        self._level  = logging.DEBUG if self._logger.name == __name__ else logging.TRACE

    async def read_byte(self, addr):
        await self.lower.write([0x01, 0x03, BDM_COMMAND_READ_BYTE, (addr >> 8) & 0xFF, addr & 0xFF, 0x01])
        byte, = await self.lower.read(1)
        return byte

    async def read_bytes(self, addr, count):
        ret = []
        for i in range(count):
            byte = await self.read_byte(addr + i)
            ret.append(byte)
        return ret


class BDMApplet(GlasgowApplet):
    logger = logging.getLogger(__name__)
    help = "communicate via BDM"
    description = """
    """


    @classmethod
    def add_build_arguments(cls, parser, access):
        super().add_build_arguments(parser, access)

        access.add_pin_argument(parser, "reset")
        access.add_pin_argument(parser, "bkgd", required=True)

        parser.add_argument(
            "--min_freq", metavar="MINFREQ", type=int, default=100000,
            help="Minimum BDM frequency to allow, should be set lower than the expected frequency (default: %(default) Hz)")
        parser.add_argument(
            "--freq", metavar="FREQ", type=int, default=None,
            help="Force a specified communication frequency, bypassing SYNC (default: %(default) Hz)")

    def build(self, target, args):
        self.__sys_clk_freq = target.sys_clk_freq

        max_cycles = math.ceil(self.__sys_clk_freq / args.min_freq)
        cycles_per_clock = None if (args.freq is None) else math.ceil(self.__sys_clk_freq / args.freq)

        self.mux_interface = iface = target.multiplexer.claim_interface(self, args)

        subtarget = iface.add_subtarget(BDM(
            ports = iface.get_port_group(
                reset = args.pin_reset,
                bkgd  = args.pin_bkgd,
            ),
            out_fifo=iface.get_out_fifo(),
            in_fifo=iface.get_in_fifo(),
            max_cycles = max_cycles,
            cycles_per_clock = cycles_per_clock,
        ))

    @classmethod
    def add_run_arguments(cls, parser, access):
        super().add_run_arguments(parser, access)

    async def run(self, device, args):
        iface = await device.demultiplexer.claim_interface(self, self.mux_interface, args)
        bdm_iface = BDMInterface(iface, self.logger)

        return bdm_iface

    @classmethod
    def add_interact_arguments(cls, parser):
        pass

    async def interact(self, device, args, bdm):
        with open("test2.bin", "wb") as file:
            data = await bdm.read_bytes(0x0000, 0xFFFF)
            file.write(bytearray(data))

