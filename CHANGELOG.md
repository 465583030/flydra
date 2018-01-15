# flydra change log

## unreleased

### Changed

* First major release as open source software.
* Reorganized code into three packages: `flydra_core`, `flydra_analysis` and
  `flydra_camnode`. Removed lots of outdated material and unused and unsupported
  code.
* Reduce the number of threads spawned by the coordinate processor thread in
  mainbrain.
* Realtime priority of coordinate processor is not elevated by default.
* Realtime priority can be set with `posix_scheduler` ROS parameter, e.g. to
  `['FIFO', 99]` to match the previous behavior.

### Added

* Make attempting to recover missed 2D data settable via environment variable
  `ATTEMPT_DATA_RECOVERY`.

### Fixed

* Documentation builds again
* Fixed a regresssion in which saving 2D data was blocked when no 3D calibration
  was loaded. Also correctly close hdf5 file in these circumstances.
* Do not quit mainbrain if a previously seen camera re-joins after suddenly
  quitting.
