Network Studio 1.2 - utility network management for QGIS
========================================================

Window sections (in the order of a project)
  OVERVIEW     Dashboard (project figures, QA report)
  1 SET UP     Network (new / demo / open / recent, backups, field project, topology)
               Model (groups, types, categories, tiers, terminals, attributes, rules, required fields)
               Attribute rules (calculation, constraint and validation rules, ready-made rules)
               Company and branding (name, logo, client, project: map book and Excel)
  2 DATA       Import (existing layers, automatic asset group matching, ArcGIS File Geodatabase:
               utility network classes, associations CSV, Network Analyst streets)
  3 EDIT       Versions (design projects: new / open version, compare, reconcile, post,
               conflicts, design status, protected Default)
               Editing (smart editing, editor tracking, upgrade)
               Asset IDs and fields (ID generator, bulk field calculator)
  4 QUALITY    Prepare and validate (gaps, vertices, 20 checks, dirty areas, exceptions, verify)
               Topology rules (14 rules between any layers: overlaps, gaps, inside, on line...)
               Associations
  5 ANALYSIS   Trace, Subnetworks, Diagrams, Profiles (sewer / storm), Criticality and risk,
               Hydraulic analysis (built-in solver: pressures, velocities, pumps, PRVs, tanks,
               24 h simulation, fire flow, calibration; low / medium pressure gas)
               Hydraulic model (EPANET / SWMM export, EPANET results back on the map)
  6 DESIGN     Service connections, Cost estimate (unit prices x quantities)
  7 OPERATIONS Work orders, Edit history (audit trail with old / new values)
  8 DELIVER    Reports (summary, BOQ, errors, data dictionary, QA), Map book (PDF),
               Web publishing (QGIS Server WMS / WFS / WFS-T, Lizmap, web editing,
               PostGIS triggers, check for edits made outside QGIS)
               ArcGIS asset package (export, plus an ArcPy script that adds the subtypes)

Several domain networks in one file / schema (New network > Add to an existing file):
the structure network is shared, every domain keeps its own model, rules and results.

Storage: GeoPackage, SpatiaLite, File Geodatabase, PostgreSQL / PostGIS (one schema per network,
several users at the same time, backups to GeoPackage).

ArcGIS Pro utility network tools and their place in Network Studio
  Create Utility Network ............ Network > New network
  Add Domain Network ................ New network > Add to an existing file
  Add / Set Network Attribute ....... Model > Network attributes
  Add / Set / Delete Network Category Model > Asset groups > Categories
  Add / Delete Rule ................. Model > Connectivity rules
  Import / Export Rules ............. Model > Rules import / export (CSV)
  Add / Set / Delete Terminal Config. Model > Terminals
  Add Tier / Add Tier Group ......... Model > Tiers and subnetwork definition
  Set Subnetwork Definition ......... Model > Tiers (many controllers, controller required)
  Set Edge Connectivity ............. Model > Categories: endvertex
  Set Association Role .............. Model > Categories: container / structure
  Enable / Disable Network Topology . Network > Network topology
  Import / Export Associations ...... Associations > Import / export (CSV)
  Import / Export Subnetwork Ctrl. .. Subnetworks > Controllers (CSV)
  Add / Delete Trace Configuration .. Trace > Saved traces
  Import / Export Trace Configs ..... Trace > Import / export (JSON)
  Add Trace Locations ............... Trace > Add start / barrier / end, selected features
  Trace ............................. Trace > Run trace (upstream also through all loop paths)
  Export Subnetwork ................. Subnetworks > Export selected subnetwork
  Repair Network Topology ........... Prepare and validate > Prepare network
  Update Is Connected ............... Subnetworks > Update Is connected
  Update Subnetwork ................. Subnetworks > Update subnetworks
  Validate Network Topology ......... Prepare and validate > Validate (full or dirty areas)
  Verify Network Topology ........... Prepare and validate > Verify
  Create / Delete Version ........... Versions > New version / Delete version
  Change Version (switch) ........... Versions > Open selected version / Back to Default
  Reconcile / Post Version .......... Versions > Reconcile / Post / Reconcile and post
  Version Changes ................... Versions > Compare with Default
  Add / Alter Attribute Rule ........ Attribute rules > Add rule / Edit
  Evaluate Rules .................... Attribute rules > Evaluate rules now

Error codes
  E01 dangling line end         E09 geometry problem (zero length, multipart)
  E02 point not connected       E10 island without a source
  E03 needs a vertex / split    E11 line tier matches no terminal
  E05 no connectivity rule      E12 size change without a fitting
  E06 empty asset group         E13 required field empty
  E07 gap                       E14 fed by more than one controller
  E08 stacked points            E15 mid-line connection on an end-vertex line
                                E16 no controller where one is required
  E17 duplicate asset id        E19 lines crossing without a junction
  E18 overlapping lines         E20 very short line
  E21 line ends at the same place on different elevation levels (F_ELEV / T_ELEV)
  AR  attribute rule broken (constraint or validation rule)
  T01-T14 general topology rules (Topology rules page)

Versions
  A version is a full copy of the network (<file>_versions/<name>.gpkg, or the schema
  <schema>_v_<name> in PostGIS), so every tool works inside it. Features are matched by
  globalid. A version can be made from another version (its parent). Features edited on
  both sides are merged field by field; only fields changed differently are conflicts.

Backups
  Automatic copies are made in <network file>_backups before prepare, import,
  service connections, model changes, associations and upgrade (the newest 20 are kept).
