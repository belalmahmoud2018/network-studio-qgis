# Network Studio for QGIS

Utility network management for QGIS: water, wastewater, stormwater, gas, electricity, fiber,
telecom, district heating / cooling and roads.

Choose the network type and Network Studio creates the classes, asset groups, asset types and
connectivity rules for it. Then import your data, validate it, trace it and deliver it.

## Features

- **Set up**: network templates, model editor (asset groups / types, categories, tiers, terminals,
  network attributes, connectivity rules), several domain networks in one file, demo networks.
- **Data**: import any layer with automatic asset group matching; import from ArcGIS
  (utility network classes, associations, Network Analyst streets).
- **Edit**: smart editing (snapping, split / vertex under new devices, live rule check),
  editor tracking, asset ID generator, bulk field calculator.
- **Quality**: automatic preparation (gaps, missing vertices), 20+ validation checks,
  dirty areas, exceptions, verify, associations.
- **Analysis**: connected, upstream, downstream, isolation (valves to close, customers affected),
  shortest path, loops, subnetworks, diagrams, sewer longitudinal profiles, criticality and risk,
  EPANET / SWMM export and EPANET results on the map.
- **Design**: automatic service connections, cost estimate.
- **Operations**: work orders, edit history with old and new values.
- **Deliver**: dashboard, QA report, bill of quantities, data dictionary, map book (PDF),
  web publishing (QGIS Server / Lizmap), ArcGIS Utility Network asset package.

## Storage

GeoPackage, SpatiaLite, File Geodatabase and PostgreSQL / PostGIS (several users at the same time).
Everything the plugin needs is stored inside the data. Asset groups and types are also written for the
[Subtypes and Domains Manager](https://github.com/belalmahmoud2018/subtypes-domains-qgis) plugin.

## Requirements

QGIS 3.28 or newer (File Geodatabase editing needs GDAL 3.6 or newer).

## Support

Please report problems in the
[issue tracker](https://github.com/belalmahmoud2018/network-studio-qgis/issues) with the text of
View > Panels > Log Messages > Network Studio.

## License

GNU General Public License v2 or later. Author: Belal Mahmoud Abdelmonem.
