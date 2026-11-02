# jmri.jmrix.* hardware system inventory

Mechanical extraction from `jmri.jar`: one row per top-level `jmri.jmrix.<system>` package. Class counts are total `.class` files under that package (including inner classes).

**Port complexity** is a judgement call (S/M/L/XL) made during Stage 0 review — left as TODO until that pass. Numbers are mechanical signals: high class count + many sub-packages + binary-protocol shape = larger port.


## Summary table

| System | Classes | Sub-pkgs | Vendor | Transport | Protocol shape | Port effort |
|---|---:|---:|---|---|---|---|
| `loconet` | 519 | 44 | Digitrax | Serial/USB/TCP | async bus broadcast | _TODO_ |
| `can` | 490 | 4 | MERG/CBUS et al. | CAN/USB | CAN frames | _TODO_ |
| `lenz` | 225 | 14 | Lenz | Serial/USB/TCP | XpressNet req/resp | _TODO_ |
| `openlcb` | 199 | 2 | OpenLCB / LCC | CAN/TCP | event-driven LCC | _TODO_ |
| `nce` | 127 | 15 | NCE | Serial/USB | sync req/resp + poll | _TODO_ |
| `cmri` | 124 | 2 | CMRI | Serial | poll/response packet | _TODO_ |
| `powerline` | 124 | 8 | X10/Insteon | Powerline | broadcast cmd | _TODO_ |
| `dccpp` | 119 | 6 | DCC-EX | Serial/TCP | text protocol | _TODO_ |
| `bidib` | 114 | 7 | BiDiB | Serial/IP | binary nodes | _TODO_ |
| `bachrus` | 107 | 5 | Bachrus | Serial | speedometer-only | _TODO_ |
| `sprog` | 105 | 13 | SPROG-DCC | Serial/USB | programmer-focused | _TODO_ |
| `rps` | 103 | 7 | JMRI RPS | Serial | ultrasonic positioning | _TODO_ |
| `roco` | 102 | 1 | Roco/Fleischmann | Serial/USB | XpressNet variant | _TODO_ |
| `ecos` | 100 | 4 | ESU ECoS | TCP | text protocol | _TODO_ |
| `ieee802154` | 81 | 3 | ZigBee/Xbee | Wireless | packet | _TODO_ |
| `srcp` | 80 | 4 | SRCP | TCP | text protocol (RFC-ish) | _TODO_ |
| `rfid` | 73 | 7 | Generic RFID | Serial | tag read events | _TODO_ |
| `ipocs` | 68 | 2 | RailComm IPOCS | TCP | ATC/CTC | _TODO_ |
| `grapevine` | 66 | 8 | ProTrak | Serial | node poll | _TODO_ |
| `marklin` | 61 | 6 | Märklin CS | TCP | CS2/CS3 IP | _TODO_ |
| `maple` | 55 | 8 | Maple Systems | Serial | PLC polling | _TODO_ |
| `tams` | 54 | 4 | TAMS | Serial | MC binary | _TODO_ |
| `mrc` | 50 | 4 | MRC | Serial | binary req/resp | _TODO_ |
| `acela` | 49 | 6 | CTI Acela | Serial | node poll | _TODO_ |
| `mqtt` | 48 | 2 | MQTT broker | TCP+MQTT | pub/sub topics | _TODO_ |
| `zimo` | 48 | 3 | Zimo MX1/10 | Serial/USB | binary req/resp | _TODO_ |
| `oaktree` | 47 | 7 | Oak Tree | Serial | node poll | _TODO_ |
| `secsi` | 47 | 7 | Tracetronics | Serial | node poll | _TODO_ |
| `easydcc` | 45 | 7 | EasyDCC | Serial | binary | _TODO_ |
| `dcc4pc` | 41 | 3 | DCC4PC | Serial/USB | RailCom reader | _TODO_ |
| `jmriclient` | 38 | 3 | JMRI bridge | TCP | JMRI-to-JMRI | _TODO_ |
| `pricom` | 37 | 2 | Pricom | Serial | binary | _TODO_ |
| `tmcc` | 34 | 6 | Lionel TMCC | Serial | binary | _TODO_ |
| `xpa` | 32 | 3 | XpressNet/PA | Serial | DTMF/phone-bridge | _TODO_ |
| `internal` | 26 | 1 | JMRI internal | in-memory | loopback / test bus | _TODO_ |
| `ncemonitor` | 24 | 0 | NCE | Serial | NCE bus sniffer | _TODO_ |
| `qsi` | 23 | 4 | QSI | Serial | programmer | _TODO_ |
| `direct` | 21 | 3 | DCC direct | Serial | raw DCC packets | _TODO_ |
| `pi` | 20 | 2 | Raspberry Pi | GPIO | GPIO sensors/turnouts | _TODO_ |
| `jinput` | 13 | 1 | JInput | USB HID | gamepad/throttle input | _TODO_ |
| `anyma` | 10 | 1 | Anyma uDMX | USB | DMX lighting | _TODO_ |
| `swing` | 10 | 0 | (UI helpers) | - | shared Swing widgets | _TODO_ |
| `ztc` | 10 | 1 | ZTC | Serial | binary | _TODO_ |
| `configurexml` | 7 | 0 | (framework) | - | shared XML config | _TODO_ |
| `purejavacomm` | 7 | 0 | (transport) | Serial | library wrapper | _TODO_ |
| `wangrow` | 6 | 1 | Wangrow | Serial | NCE-compatible | _TODO_ |
| `jserialcomm` | 4 | 0 | (transport) | Serial | library wrapper | _TODO_ |
| `debugthrottle` | 2 | 0 | (test) | - | test fixture | _TODO_ |
| `fakeport` | 2 | 0 | (test) | - | test fixture | _TODO_ |
| `libusb` | 2 | 0 | (transport) | USB | library wrapper | _TODO_ |
| `merg` | 1 | 0 | _TODO_ | _TODO_ | _TODO_ | _TODO_ |
| `sproggen5` | 1 | 0 | _TODO_ | _TODO_ | _TODO_ | _TODO_ |

## Per-system sub-package breakdown


### `loconet` — 519 classes, 44 sub-packages — Digitrax

| Sub-package | Classes |
|---|---:|
| `loconet.logixng` | 30 |
| `loconet.duplexgroup` | 27 |
| `loconet.sdfeditor` | 24 |
| `loconet.swing` | 24 |
| `loconet.uhlenbrock` | 23 |
| `loconet.loconetovertcp` | 20 |
| `loconet.sdf` | 19 |
| `loconet.ds64` | 15 |
| `loconet.soundloader` | 15 |
| `loconet.downloader` | 14 |
| `loconet.alm` | 12 |
| `loconet.hexfile` | 12 |
| `loconet.Intellibox` | 10 |
| `loconet.locostats` | 10 |
| `loconet.pr3` | 10 |
| `loconet.configurexml` | 9 |
| `loconet.pr4` | 8 |
| `loconet.usb_dcs210Plus` | 8 |
| `loconet.usb_dcs240` | 8 |
| `loconet.usb_dcs240Plus` | 8 |
| `loconet.usb_dcs52` | 8 |
| `loconet.locoio` | 7 |
| `loconet.pr2` | 7 |
| `loconet.slotmon` | 7 |
| `loconet.demoport` | 6 |
| `loconet.streamport` | 6 |
| `loconet.bluetooth` | 5 |
| `loconet.lnsvf1` | 5 |
| `loconet.clockmon` | 4 |
| `loconet.locobuffer` | 4 |
| `loconet.locobufferii` | 4 |
| `loconet.locobufferng` | 4 |
| `loconet.locobufferusb` | 4 |
| `loconet.locoid` | 4 |
| `loconet.ms100` | 4 |
| `loconet.lnsvf2` | 3 |
| `loconet.locogen` | 3 |
| `loconet.locomon` | 3 |
| `loconet.spjfile` | 3 |
| `loconet.bdl16` | 2 |
| `loconet.cmdstnconfig` | 2 |
| `loconet.messageinterp` | 2 |
| `loconet.pm4` | 2 |
| `loconet.se8` | 2 |

### `can` — 490 classes, 4 sub-packages — MERG/CBUS et al.

| Sub-package | Classes |
|---|---:|
| `can.cbus` | 377 |
| `can.adapters` | 78 |
| `can.swing` | 13 |
| `can.nmranet` | 6 |

### `lenz` — 225 classes, 14 sub-packages — Lenz

| Sub-package | Classes |
|---|---:|
| `lenz.messageformatters` | 67 |
| `lenz.swing` | 36 |
| `lenz.hornbyelite` | 15 |
| `lenz.liusbserver` | 8 |
| `lenz.xntcp` | 7 |
| `lenz.li100` | 6 |
| `lenz.configurexml` | 5 |
| `lenz.liusb` | 5 |
| `lenz.liusbethernet` | 5 |
| `lenz.ztc640` | 5 |
| `lenz.li100f` | 4 |
| `lenz.li101` | 4 |
| `lenz.lzv200` | 4 |
| `lenz.xnetsimulator` | 4 |

### `openlcb` — 199 classes, 2 sub-packages — OpenLCB / LCC

| Sub-package | Classes |
|---|---:|
| `openlcb.swing` | 125 |
| `openlcb.configurexml` | 10 |

### `nce` — 127 classes, 15 sub-packages — NCE

| Sub-package | Classes |
|---|---:|
| `nce.consist` | 15 |
| `nce.macro` | 13 |
| `nce.cab` | 9 |
| `nce.boosterprog` | 8 |
| `nce.swing` | 6 |
| `nce.ph5driver` | 5 |
| `nce.usbdriver` | 5 |
| `nce.ncemon` | 4 |
| `nce.serialdriver` | 4 |
| `nce.simulator` | 4 |
| `nce.usbinterface` | 4 |
| `nce.clockmon` | 3 |
| `nce.configurexml` | 3 |
| `nce.networkdriver` | 3 |
| `nce.packetgen` | 3 |

### `cmri` — 124 classes, 2 sub-packages — CMRI

| Sub-package | Classes |
|---|---:|
| `cmri.serial` | 117 |
| `cmri.swing` | 2 |

### `powerline` — 124 classes, 8 sub-packages — X10/Insteon

| Sub-package | Classes |
|---|---:|
| `powerline.swing` | 16 |
| `powerline.insteon2412s` | 15 |
| `powerline.simulator` | 15 |
| `powerline.cm11` | 14 |
| `powerline.cp290` | 14 |
| `powerline.dmx512` | 12 |
| `powerline.serialdriver` | 4 |
| `powerline.configurexml` | 3 |

### `dccpp` — 119 classes, 6 sub-packages — DCC-EX

| Sub-package | Classes |
|---|---:|
| `dccpp.swing` | 38 |
| `dccpp.dccppovertcp` | 18 |
| `dccpp.network` | 6 |
| `dccpp.serial` | 6 |
| `dccpp.configurexml` | 5 |
| `dccpp.simulator` | 5 |

### `bidib` — 114 classes, 7 sub-packages — BiDiB

| Sub-package | Classes |
|---|---:|
| `bidib.swing` | 17 |
| `bidib.netbidib` | 11 |
| `bidib.tcpserver` | 10 |
| `bidib.serialdriver` | 9 |
| `bidib.simulator` | 6 |
| `bidib.configurexml` | 5 |
| `bidib.bidibovertcp` | 4 |

### `bachrus` — 107 classes, 5 sub-packages — Bachrus

| Sub-package | Classes |
|---|---:|
| `bachrus.speedmatcher` | 63 |
| `bachrus.drmserialdriver` | 5 |
| `bachrus.kpfserialdriver` | 5 |
| `bachrus.serialdriver` | 5 |
| `bachrus.swing` | 2 |

### `sprog` — 105 classes, 13 sub-packages — SPROG-DCC

| Sub-package | Classes |
|---|---:|
| `sprog.update` | 17 |
| `sprog.pi` | 13 |
| `sprog.sprogslotmon` | 8 |
| `sprog.console` | 5 |
| `sprog.serialdriver` | 5 |
| `sprog.packetgen` | 4 |
| `sprog.simulator` | 4 |
| `sprog.sprogCS` | 4 |
| `sprog.sprognano` | 4 |
| `sprog.sprogmon` | 3 |
| `sprog.configurexml` | 2 |
| `sprog.sprog` | 2 |
| `sprog.swing` | 1 |

### `rps` — 103 classes, 7 sub-packages — JMRI RPS

| Sub-package | Classes |
|---|---:|
| `rps.swing` | 26 |
| `rps.trackingpanel` | 11 |
| `rps.aligntable` | 10 |
| `rps.serial` | 6 |
| `rps.reversealign` | 4 |
| `rps.configurexml` | 2 |
| `rps.rpsmon` | 2 |

### `roco` — 102 classes, 1 sub-packages — Roco/Fleischmann

| Sub-package | Classes |
|---|---:|
| `roco.z21` | 97 |

### `ecos` — 100 classes, 4 sub-packages — ESU ECoS

| Sub-package | Classes |
|---|---:|
| `ecos.swing` | 32 |
| `ecos.utilities` | 20 |
| `ecos.configurexml` | 5 |
| `ecos.networkdriver` | 4 |

### `ieee802154` — 81 classes, 3 sub-packages — ZigBee/Xbee

| Sub-package | Classes |
|---|---:|
| `ieee802154.xbee` | 39 |
| `ieee802154.swing` | 25 |
| `ieee802154.serialdriver` | 7 |

### `srcp` — 80 classes, 4 sub-packages — SRCP

| Sub-package | Classes |
|---|---:|
| `srcp.parser` | 41 |
| `srcp.swing` | 12 |
| `srcp.networkdriver` | 3 |
| `srcp.configurexml` | 2 |

### `rfid` — 73 classes, 7 sub-packages — Generic RFID

| Sub-package | Classes |
|---|---:|
| `rfid.swing` | 17 |
| `rfid.generic` | 7 |
| `rfid.merg` | 7 |
| `rfid.protocol` | 6 |
| `rfid.serialdriver` | 6 |
| `rfid.networkdriver` | 5 |
| `rfid.configurexml` | 3 |

### `ipocs` — 68 classes, 2 sub-packages — RailComm IPOCS

| Sub-package | Classes |
|---|---:|
| `ipocs.protocol` | 41 |
| `ipocs.configurexml` | 4 |

### `grapevine` — 66 classes, 8 sub-packages — ProTrak

| Sub-package | Classes |
|---|---:|
| `grapevine.nodetable` | 13 |
| `grapevine.nodeconfig` | 10 |
| `grapevine.packetgen` | 6 |
| `grapevine.configurexml` | 4 |
| `grapevine.serialdriver` | 4 |
| `grapevine.simulator` | 4 |
| `grapevine.serialmon` | 3 |
| `grapevine.swing` | 2 |

### `marklin` — 61 classes, 6 sub-packages — Märklin CS

| Sub-package | Classes |
|---|---:|
| `marklin.swing` | 17 |
| `marklin.cdb` | 9 |
| `marklin.simulation` | 6 |
| `marklin.networkdriver` | 3 |
| `marklin.tcpdriver` | 3 |
| `marklin.configurexml` | 2 |

### `maple` — 55 classes, 8 sub-packages — Maple Systems

| Sub-package | Classes |
|---|---:|
| `maple.nodeconfig` | 9 |
| `maple.assignment` | 8 |
| `maple.serialdriver` | 4 |
| `maple.simulator` | 4 |
| `maple.configurexml` | 3 |
| `maple.packetgen` | 3 |
| `maple.serialmon` | 3 |
| `maple.swing` | 1 |

### `tams` — 54 classes, 4 sub-packages — TAMS

| Sub-package | Classes |
|---|---:|
| `tams.swing` | 23 |
| `tams.serialdriver` | 4 |
| `tams.simulator` | 4 |
| `tams.configurexml` | 2 |

### `mrc` — 50 classes, 4 sub-packages — MRC

| Sub-package | Classes |
|---|---:|
| `mrc.swing` | 14 |
| `mrc.serialdriver` | 4 |
| `mrc.simulator` | 3 |
| `mrc.configurexml` | 1 |

### `acela` — 49 classes, 6 sub-packages — CTI Acela

| Sub-package | Classes |
|---|---:|
| `acela.nodeconfig` | 14 |
| `acela.configurexml` | 4 |
| `acela.packetgen` | 4 |
| `acela.serialdriver` | 4 |
| `acela.acelamon` | 3 |
| `acela.swing` | 1 |

### `mqtt` — 48 classes, 2 sub-packages — MQTT broker

| Sub-package | Classes |
|---|---:|
| `mqtt.logixng` | 14 |
| `mqtt.configurexml` | 6 |

### `zimo` — 48 classes, 3 sub-packages — Zimo MX1/10

| Sub-package | Classes |
|---|---:|
| `zimo.swing` | 13 |
| `zimo.mx1` | 4 |
| `zimo.mxulf` | 4 |

### `oaktree` — 47 classes, 7 sub-packages — Oak Tree

| Sub-package | Classes |
|---|---:|
| `oaktree.nodeconfig` | 9 |
| `oaktree.packetgen` | 5 |
| `oaktree.serialdriver` | 4 |
| `oaktree.simulator` | 4 |
| `oaktree.configurexml` | 3 |
| `oaktree.serialmon` | 3 |
| `oaktree.swing` | 1 |

### `secsi` — 47 classes, 7 sub-packages — Tracetronics

| Sub-package | Classes |
|---|---:|
| `secsi.nodeconfig` | 9 |
| `secsi.packetgen` | 5 |
| `secsi.serialdriver` | 4 |
| `secsi.simulator` | 4 |
| `secsi.configurexml` | 3 |
| `secsi.serialmon` | 3 |
| `secsi.swing` | 1 |

### `easydcc` — 45 classes, 7 sub-packages — EasyDCC

| Sub-package | Classes |
|---|---:|
| `easydcc.simulator` | 5 |
| `easydcc.networkdriver` | 4 |
| `easydcc.packetgen` | 4 |
| `easydcc.serialdriver` | 4 |
| `easydcc.easydccmon` | 3 |
| `easydcc.configurexml` | 1 |
| `easydcc.swing` | 1 |

### `dcc4pc` — 41 classes, 3 sub-packages — DCC4PC

| Sub-package | Classes |
|---|---:|
| `dcc4pc.swing` | 15 |
| `dcc4pc.serialdriver` | 3 |
| `dcc4pc.configurexml` | 2 |

### `jmriclient` — 38 classes, 3 sub-packages — JMRI bridge

| Sub-package | Classes |
|---|---:|
| `jmriclient.swing` | 11 |
| `jmriclient.networkdriver` | 5 |
| `jmriclient.configurexml` | 4 |

### `pricom` — 37 classes, 2 sub-packages — Pricom

| Sub-package | Classes |
|---|---:|
| `pricom.pockettester` | 26 |
| `pricom.downloader` | 9 |

### `tmcc` — 34 classes, 6 sub-packages — Lionel TMCC

| Sub-package | Classes |
|---|---:|
| `tmcc.packetgen` | 4 |
| `tmcc.serialdriver` | 4 |
| `tmcc.simulator` | 4 |
| `tmcc.serialmon` | 3 |
| `tmcc.configurexml` | 1 |
| `tmcc.swing` | 1 |

### `xpa` — 32 classes, 3 sub-packages — XpressNet/PA

| Sub-package | Classes |
|---|---:|
| `xpa.swing` | 13 |
| `xpa.serialdriver` | 4 |
| `xpa.configurexml` | 1 |

### `internal` — 26 classes, 1 sub-packages — JMRI internal

| Sub-package | Classes |
|---|---:|
| `internal.configurexml` | 8 |

### `qsi` — 23 classes, 4 sub-packages — QSI

| Sub-package | Classes |
|---|---:|
| `qsi.packetgen` | 3 |
| `qsi.qsimon` | 3 |
| `qsi.serialdriver` | 3 |
| `qsi.swing` | 2 |

### `direct` — 21 classes, 3 sub-packages — DCC direct

| Sub-package | Classes |
|---|---:|
| `direct.serial` | 4 |
| `direct.simulator` | 4 |
| `direct.swing` | 1 |

### `pi` — 20 classes, 2 sub-packages — Raspberry Pi

| Sub-package | Classes |
|---|---:|
| `pi.simulator` | 7 |
| `pi.configurexml` | 4 |

### `jinput` — 13 classes, 1 sub-packages — JInput

| Sub-package | Classes |
|---|---:|
| `jinput.treecontrol` | 9 |

### `anyma` — 10 classes, 1 sub-packages — Anyma uDMX

| Sub-package | Classes |
|---|---:|
| `anyma.configurexml` | 2 |

### `ztc` — 10 classes, 1 sub-packages — ZTC

| Sub-package | Classes |
|---|---:|
| `ztc.ztc611` | 8 |

### `wangrow` — 6 classes, 1 sub-packages — Wangrow

| Sub-package | Classes |
|---|---:|
| `wangrow.serialdriver` | 4 |

## Notes for the Stage 0 review pass

For each system, fill in **Port effort** (S = days, M = 1–2 weeks, L = 3–6 weeks, XL = 2+ months) based on: protocol complexity, hardware availability for testing, presence of a Python equivalent library, and how much of the package is shared infrastructure vs. unique protocol logic.

Recommended port-order anchors:
1. `internal` (already done in Stage 1)
2. `loconet` — sets the async-push pattern
3. `nce` — sets the sync-poll pattern
4. `dccpp` — modern text protocol, quick win, large user base
5. `openlcb` — event-driven, network-native, very different shape
6. `mqtt` — pub/sub broker pattern
7. _Remaining 38 systems_ — community-driven from here, each a self-contained PyPI package.
