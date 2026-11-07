# Python Model Railroad Interface

## Overview
This is a native python re-implementation of the Java Model Railroad Interface (JMRI) based on JMRI version 5.14. The implementation uses python, JSON, html and javascript. The interface is rendered using a web browser and a native python web server. The purpose of this work was to move to a more modern infrastructure that is more accessible to contributors, as python is far easier to learn, code, and debug that java. Furthermore, there is a dramatic reduction in the number of classes and methods, improving overall performance, particularly on slower hardware such as a raspberry pi. It also allows for user scripts written in python3 natively, and eliminates the dependency on jython which is effectively dead due to being locked to python2.7.

This implementation if fully compatible with the JMRI implementation. It reads and writes all the existing xml profiles in the user working directory. As such, you can switch between PMRI and JMRI without loss of information. However, unlike JMRI, there is a single interface which incorporates both PanelPro and DecoderPro functionality. There are some enhancements, like support for multiple connections. So, if you use and NCE command station for operations and some other device, like an SPROG2 or dccex board for decoder programming, there is one profile directory (roster.xml, decoder settings, etc.) and you can switch between connections to the different devices.

## Structure
pmri is the base engine that includes all the core functions for managing all the connections and operation of the web interface. Support for different command station hardware is treated as a plug-in and are installed separately from the pmri base. There is a well defined hardware abstraction layer (HAL) by which all the command station interfaces are implemented.

### Basic Component Overview

- pmri-core:
Handles persistence of all the settings from the various xml files and the connection manager between the command station hardware and the JSON server.
- pmri-server:
Essentially the JSON server, connecting between pmri-core and pmri-web
- pmri-web:
Runs the web server and renders the interface, sending request back through pmri-server.pmri-scripting:
Executes user defined scripts and provides an API interface. In addition, there is a jmri shim that allows for direct execution of existing jython based python scripts. Depending on the script, some modification may be required to work with python 3.x, mostly related to print statement format.
- pmri-decoderpro:
decoder programming interface
- pmri-openlcb:
LCC implementation support
- pmri-logixng:
The logixng support for layout automation.
- pmri-loconet:
The loconet network protocol support
- pmri-withrottle:
The wifi server for using throttle software on android/ios devices
- pmri-pmri:
The launcher for pmri to configure the settings and start the server and web interface

## Supported command stations
There is a hardware plugin for nce (pmri-nce) and dccex (pmri-dccex). There are stubs for digitrax, lenz, and mrc that plugin, but require implementation and debug.

## Status
The existing release has been fully tested for both operation and decoder programming with an NCE PowerPro with a 2006+ eprom and a dccex command station. In addition, withrottle has been fully tested with android and the EngineDriver app. The scripting environment is also debugged. It has been run on Windows11, Linux (ubuntu and mint), and raspbian OS on a raspberry pi 3B.

## Installation

Python 3.12 or later is required. Support for the case statement is necessary.

To install pmri:
- pip install pmri

Installation of command station support:
- pip install pmri-nce
- pip install pmri-dccex

Note that you can install as many command station plugins as desired. They won't be used unless specfied as the active connection

Note also, if you do "pip install pmri-nce", it will also automatically install pmri if hasn't been already installed since it is a dependency.

## Execution

Once installed:
- pmri command will launch the gui to set the options and manage the web service.
- pmri --headless will start the server directly without the pmri launch gui or web server (background operation on a raspberry pi, for example) 

Note:
Presently, this depends on the decoder and signal xml files from JMRI. These are not currently bundled with pmri. 

On linux/raspbian, these are normally located here:
- /home/_user_/JMRI/xml/signals
- /home/_user_/JMRI/xml/decoders

On windows: 
- C:\Program Files (x86)\JMRI\xml\decoders
- C:\Program Files (x86)\JMRI\xml\signals
