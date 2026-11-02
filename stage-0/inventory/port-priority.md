# Package dependency graph & port-priority hint

Extracted from `jmri.jar` by parsing class-file constant pools.
Depth: 3 (package granularity).
Classes scanned: **10127**, packages: **178**, edges: **1577**, parse errors: 0.


## Port-priority layers (topological)

Each layer contains packages whose *internal* dependencies are all in earlier layers. Layer 0 is leaf packages — port these first because nothing else inside JMRI depends on them transitively.

Where multiple packages share a layer, they're independent and can be ported in parallel. Cycles are collapsed into strongly-connected components (SCCs) — packages in the same SCC must be ported together.


### Layer 0

- `apps.DecoderPro` (6 classes)
- `apps.DispatcherPro` (3 classes)
- `apps.InstallTest` (3 classes)
- `apps.PanelPro` (6 classes)
- `apps.SoundPro` (3 classes)
- `apps.configurexml` (4 classes)
- `apps.gui3.dp3` (13 classes)
- `apps.gui3.lccpro` (6 classes)
- `apps.gui3.mdi` (1 classes)
- `apps.gui3.paned` (4 classes)
- `apps.jmrit.decoderdefn` (1 classes)
- `apps.startup.configurexml` (5 classes)
- `jmri.jmrit.etcs` (32 classes)
- `jmri.jmrit.mastbuilder` (3 classes)
- `jmri.jmrit.sample` (8 classes)
- `jmri.jmrit.signalsystemeditor` (18 classes)
- `jmri.jmrit.tracker` (2 classes)
- `jmri.jmrix.anyma` (10 classes)
- `jmri.jmrix.bachrus` (107 classes)
- `jmri.jmrix.bidib` (114 classes)
- `jmri.jmrix.cmri` (124 classes)
- `jmri.jmrix.dcc4pc` (41 classes)
- `jmri.jmrix.dccpp` (119 classes)
- `jmri.jmrix.direct` (21 classes)
- `jmri.jmrix.easydcc` (45 classes)
- `jmri.jmrix.ecos` (100 classes)
- `jmri.jmrix.ieee802154` (81 classes)
- `jmri.jmrix.ipocs` (68 classes)
- `jmri.jmrix.jmriclient` (38 classes)
- `jmri.jmrix.maple` (55 classes)
- `jmri.jmrix.marklin` (61 classes)
- `jmri.jmrix.mqtt` (48 classes)
- `jmri.jmrix.mrc` (50 classes)
- `jmri.jmrix.ncemonitor` (24 classes)
- `jmri.jmrix.oaktree` (47 classes)
- `jmri.jmrix.pi` (20 classes)
- `jmri.jmrix.powerline` (124 classes)
- `jmri.jmrix.qsi` (23 classes)
- `jmri.jmrix.rfid` (73 classes)
- `jmri.jmrix.secsi` (47 classes)
- `jmri.jmrix.sprog` (105 classes)
- `jmri.jmrix.sproggen5` (1 classes)
- `jmri.jmrix.srcp` (80 classes)
- `jmri.jmrix.tams` (54 classes)
- `jmri.jmrix.tmcc` (34 classes)
- `jmri.jmrix.wangrow` (6 classes)
- `jmri.jmrix.xpa` (32 classes)
- `jmri.jmrix.zimo` (48 classes)
- `jmri.jmrix.ztc` (10 classes)
- `jmri.util.docbook` (3 classes)

### Layer 1

- `apps.startup` (20 classes)
- `purejavacomm` (0 classes)
- `jmri.implementation.decorators` (3 classes)

### Layer 2

- **SCC** (100 classes across 10 pkgs — port together):
    - `apps` (41 classes)
    - `apps.TrainCrew` (3 classes)
    - `apps.gui3` (7 classes)
    - `apps.gui3.tabbedpreferences` (13 classes)
    - `apps.jmrit` (5 classes)
    - `apps.jmrit.log` (6 classes)
    - `apps.swing` (4 classes)
    - `apps.systemconsole` (1 classes)
    - `apps.util` (3 classes)
    - `apps.util.issuereporter` (17 classes)

### Layer 3

- `jmri.jmrix.jinput` (13 classes)
- `jmri.jmrix.libusb` (2 classes)
- `jmri.util.exceptionhandler` (3 classes)
- `jmri.jmrix.pricom` (37 classes)
- `apps.plaf.macosx` (6 classes)
- `jmri.jmrit.permission` (16 classes)

### Layer 4

- `net.java.games` (0 classes)
- **SCC** (7971 classes across 103 pkgs — port together):
    - `jmri` (218 classes)
    - `jmri.beans` (21 classes)
    - `jmri.configurexml` (37 classes)
    - `jmri.configurexml.swing` (8 classes)
    - `jmri.configurexml.turnoutoperations` (5 classes)
    - `jmri.implementation` (136 classes)
    - `jmri.implementation.configurexml` (15 classes)
    - `jmri.implementation.swing` (2 classes)
    - `jmri.jmris` (32 classes)
    - `jmri.jmris.simpleserver` (57 classes)
    - `jmri.jmris.srcp` (82 classes)
    - `jmri.jmrit` (33 classes)
    - `jmri.jmrit.analogclock` (7 classes)
    - `jmri.jmrit.audio` (40 classes)
    - `jmri.jmrit.automat` (21 classes)
    - `jmri.jmrit.beantable` (318 classes)
    - `jmri.jmrit.blockboss` (8 classes)
    - `jmri.jmrit.cabsignals` (7 classes)
    - `jmri.jmrit.catalog` (35 classes)
    - `jmri.jmrit.conditional` (102 classes)
    - `jmri.jmrit.consisttool` (13 classes)
    - `jmri.jmrit.ctc` (250 classes)
    - `jmri.jmrit.decoderdefn` (15 classes)
    - `jmri.jmrit.dispatcher` (80 classes)
    - `jmri.jmrit.display` (761 classes)
    - `jmri.jmrit.dualdecoder` (9 classes)
    - `jmri.jmrit.entryexit` (35 classes)
    - `jmri.jmrit.jython` (12 classes)
    - `jmri.jmrit.lcdclock` (6 classes)
    - `jmri.jmrit.logix` (132 classes)
    - `jmri.jmrit.logixng` (1303 classes)
    - `jmri.jmrit.mailreport` (6 classes)
    - `jmri.jmrit.nixieclock` (6 classes)
    - `jmri.jmrit.operations` (505 classes)
    - `jmri.jmrit.picker` (26 classes)
    - `jmri.jmrit.powerpanel` (9 classes)
    - `jmri.jmrit.pragotronclock` (5 classes)
    - `jmri.jmrit.progsupport` (10 classes)
    - `jmri.jmrit.revhistory` (7 classes)
    - `jmri.jmrit.roster` (123 classes)
    - `jmri.jmrit.sendpacket` (7 classes)
    - `jmri.jmrit.sensorgroup` (6 classes)
    - `jmri.jmrit.signalling` (33 classes)
    - `jmri.jmrit.simpleclock` (9 classes)
    - `jmri.jmrit.simplelightctrl` (11 classes)
    - `jmri.jmrit.simpleprog` (4 classes)
    - `jmri.jmrit.simpleturnoutctrl` (4 classes)
    - `jmri.jmrit.speedometer` (15 classes)
    - `jmri.jmrit.swing` (15 classes)
    - `jmri.jmrit.symbolicprog` (206 classes)
    - `jmri.jmrit.throttle` (97 classes)
    - `jmri.jmrit.timetable` (43 classes)
    - `jmri.jmrit.turnoutoperations` (7 classes)
    - `jmri.jmrit.ussctc` (56 classes)
    - `jmri.jmrit.vsdecoder` (122 classes)
    - `jmri.jmrit.whereused` (21 classes)
    - `jmri.jmrit.withrottle` (42 classes)
    - `jmri.jmrit.z21server` (15 classes)
    - `jmri.jmrix` (86 classes)
    - `jmri.jmrix.acela` (49 classes)
    - `jmri.jmrix.can` (490 classes)
    - `jmri.jmrix.configurexml` (7 classes)
    - `jmri.jmrix.debugthrottle` (2 classes)
    - `jmri.jmrix.fakeport` (2 classes)
    - `jmri.jmrix.grapevine` (66 classes)
    - `jmri.jmrix.internal` (26 classes)
    - `jmri.jmrix.jserialcomm` (4 classes)
    - `jmri.jmrix.lenz` (225 classes)
    - `jmri.jmrix.loconet` (519 classes)
    - `jmri.jmrix.merg` (1 classes)
    - `jmri.jmrix.nce` (127 classes)
    - `jmri.jmrix.openlcb` (199 classes)
    - `jmri.jmrix.purejavacomm` (7 classes)
    - `jmri.jmrix.roco` (102 classes)
    - `jmri.jmrix.rps` (103 classes)
    - `jmri.jmrix.swing` (10 classes)
    - `jmri.managers` (58 classes)
    - `jmri.managers.configurexml` (25 classes)
    - `jmri.profile` (55 classes)
    - `jmri.progdebugger` (7 classes)
    - `jmri.script` (6 classes)
    - `jmri.script.configurexml` (1 classes)
    - `jmri.script.swing` (7 classes)
    - `jmri.server` (1 classes)
    - `jmri.server.json` (164 classes)
    - `jmri.server.web` (14 classes)
    - `jmri.spi` (4 classes)
    - `jmri.swing` (42 classes)
    - `jmri.util` (103 classes)
    - `jmri.util.datatransfer` (1 classes)
    - `jmri.util.davidflanagan` (3 classes)
    - `jmri.util.gui` (1 classes)
    - `jmri.util.node` (2 classes)
    - `jmri.util.org` (7 classes)
    - `jmri.util.prefs` (17 classes)
    - `jmri.util.startup` (23 classes)
    - `jmri.util.swing` (102 classes)
    - `jmri.util.table` (15 classes)
    - `jmri.util.usb` (12 classes)
    - `jmri.util.zeroconf` (9 classes)
    - `jmri.web` (1 classes)
    - `jmri.web.server` (12 classes)
    - `jmri.web.servlet` (44 classes)

### Layer 5

- `jmri.util.jdom` (2 classes)
- `jmri.util.iharder` (10 classes)
- `jmri.util.xml` (3 classes)
- `jmri.jmrit.sound` (2 classes)
- `jmri.util.com` (6 classes)
- `net.coobird.thumbnailator` (0 classes)
- `jmri.util.javaworld` (1 classes)
- `jmri.util.problemreport` (1 classes)

## Top fan-out packages (depend on many others)

| Package | Outgoing edges |
|---|---:|
| `jmri.jmrit` | 43 |
| `apps` | 37 |
| `jmri.jmrit.beantable` | 34 |
| `jmri.jmrit.display` | 30 |
| `jmri.jmrix.loconet` | 29 |
| `jmri.jmrix.can` | 27 |
| `jmri.implementation` | 25 |
| `jmri.jmrit.logixng` | 25 |
| `jmri.jmrit.operations` | 25 |
| `jmri.managers` | 23 |
| `jmri.server.json` | 22 |
| `jmri.jmrit.roster` | 21 |
| `jmri.jmrix.openlcb` | 21 |
| `jmri.jmrit.logix` | 20 |
| `jmri.jmrix` | 20 |
| `jmri.jmrit.throttle` | 19 |
| `jmri.jmrix.ecos` | 19 |
| `jmri.jmrit.symbolicprog` | 18 |
| `apps.util.issuereporter` | 17 |
| `jmri.web.servlet` | 17 |

## Top fan-in packages (depended on by many others)

| Package | Incoming edges |
|---|---:|
| `jmri` | 154 |
| `jmri.util` | 141 |
| `jmri.util.swing` | 115 |
| `jmri.jmrix` | 72 |
| `jmri.implementation` | 68 |
| `jmri.jmrit` | 68 |
| `jmri.configurexml` | 64 |
| `jmri.util.startup` | 50 |
| `jmri.managers` | 49 |
| `jmri.swing` | 46 |
| `jmri.managers.configurexml` | 43 |
| `jmri.jmrix.configurexml` | 38 |
| `jmri.jmrix.swing` | 36 |
| `jmri.profile` | 35 |
| `jmri.jmrit.roster` | 34 |
| `jmri.beans` | 29 |
| `jmri.util.prefs` | 22 |
| `jmri.util.table` | 22 |
| `jmri.jmrit.display` | 20 |
| `jmri.jmrit.symbolicprog` | 20 |