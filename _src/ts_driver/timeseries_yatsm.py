# -*- coding: utf-8 -*-
# vim: set expandtab:ts=4
"""
/***************************************************************************
 Yet Another TimeSeries Model
                                 A QGIS plugin
 Plugin for visualization and analysis of remote sensing time series
                             -------------------
        begin                : 2013-03-15
        copyright            : (C) 2013 by Chris Holden
        email                : ceholden@gmail.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
from datetime import datetime as dt
import fnmatch
import logging
import os
import re

import numpy as np
from osgeo import gdal
import patsy

from . import parse_landsat_MTL
import timeseries_ccdc

logger = logging.getLogger('tstools')


class YATSM_LIVE(timeseries_ccdc.CCDCTimeSeries):
    """ Timeseries "driver" for QGIS plugin that connects requests with model
    """

    # description name for TSTools data model plugin loader
    description = 'YATSM Plotter'

    results_folder = 'YATSM'
    results_pattern = 'yatsm_r*'

    results_folder = 'YATSM'
    results_pattern = 'yatsm_r*'
    min_values = [0]
    max_values = [10000]
    metadata_file_pattern = 'L*MTL.txt'

    configurable = ['image_pattern',
                    'stack_pattern',
                    'cache_folder',
                    'results_folder',
                    'results_pattern',
                    'mask_band',
                    'min_values', 'max_values',
                    'metadata_file_pattern']

    configurable_str = ['Image folder pattern',
                        'Stack pattern',
                        'Cache folder',
                        'Results folder (if any)',
                        'Results file pattern (if any)',
                        'Mask band',
                        'Min data values', 'Max data values',
                        'Metadata file pattern']

    calculate_live = True
    consecutive = 5
    min_obs = 16
    threshold = 3.0
    enable_min_rmse = True
    min_rmse = 100.0
#    freq = np.array([1])
    design = '1 + x + harm(x, 1)'
    reverse = False
    screen_lowess = False
    screen_crit = 400.0
    remove_noise = True
    dynamic_rmse = False
    test_indices = np.array([2, 3, 4, 5])
    robust_results = False
    commit_test = False
    commit_alpha = 0.01
    debug = False

    custom_controls_title = 'YATSM Options'
    custom_controls = ['calculate_live',
                       'consecutive', 'min_obs', 'threshold',
                       'enable_min_rmse', 'min_rmse',
                       'design', 'reverse',
                       'screen_lowess', 'screen_crit', 'remove_noise',
                       'dynamic_rmse',
                       'test_indices', 'robust_results',
                       'commit_test', 'commit_alpha',
                       'debug']

    cloud_cover = np.empty(0)
    sensor = np.empty(0)
    pathrow = np.empty(0)
    multitemp_screened = np.empty(0)

    metadata_bool = [True, False, False, False]
    metadata = ['cloud_cover', 'sensor', 'pathrow', 'multitemp_screened']
    metadata_str = ['Cloud Cover', 'Sensor', 'Path/Row', 'Multitemporal Screen']

    def __init__(self, location, config=None):
        # Monkey patch CCDCTimeSeries._find_stacks to also find image metadata
        old_find_stacks = timeseries_ccdc.CCDCTimeSeries._find_stacks
        def patch_find_stacks(self):
            old_find_stacks(self)
            if hasattr(self, '_find_metadata'):
                self._find_metadata()
        timeseries_ccdc.CCDCTimeSeries._find_stacks = patch_find_stacks

        super(YATSM_LIVE, self).__init__(location, config)

        # Find metadata
        self._find_metadata()

        self.ord_dates = np.array(map(dt.toordinal, self.dates))
        self.X = None
        self.Y = None
        self._check_yatsm()

        if len(self.min_values) == 1:
            self.min_values = self.min_values * (self.n_band - 1)
        if len(self.max_values) == 1:
            self.max_values = self.max_values * (self.n_band - 1)
        self.min_values = np.asarray(self.min_values)
        self.max_values = np.asarray(self.max_values)

    def set_custom_controls(self, values):
        """ Set custom control options

        Arguments:
            values          list of values to be inserted into OrderedDict

        """
        for v, k in zip(values, self.custom_controls):
            current_value = getattr(self, k)
            if isinstance(v, type(current_value)):
                setattr(self, k, v)
            else:
                # Make an exception for minimum RMSE since we can pass None
                if k == 'min_rmse' and isinstance(v, float):
                    setattr(self, k, v)
                else:
                    print 'Error setting value for {o}'.format(o=k)
                    print current_value, v

    def retrieve_result(self):
        """ Returns the results either calculated live or from a model run
        """
        logger.info('Calculating live?: {b}'.format(b=self.calculate_live))
        if self.calculate_live:
            self._retrieve_result_live()
        else:
            self._retrieve_result_saved()

        # Update multitemporal screening metadata
        if self.yatsm_model:
            self.multitemp_screened = np.in1d(self.X[:, 1],
                                              self.yatsm_model.X[:, 1],
                                              invert=True).astype(np.uint8)

    def _retrieve_result_live(self):
        """ Returns the record changes for the current pixel
        """
        # Note: X recalculated during variable setting, if needed, unless None
        # self.X = make_X(self.ord_dates, self.freq).T
        self.X = patsy.dmatrix(self.design,
                               {'x': self.ord_dates,
                                'sensor': self.sensor,
                                'pr': self.pathrow})
        self.design_info = self.X.design_info

        # Get Y
        self.Y = self.get_data(mask=False)

        # Mask out mask values
        clear = np.logical_and.reduce([self.Y[self.mask_band - 1, :] != mv
                                      for mv in self.mask_val])
        valid = get_valid_mask(self.Y[:self.mask_band - 1, :],
                               self.min_values,
                               self.max_values)
        clear *= valid

        # Turn on/off minimum RMSE
        if not self.enable_min_rmse:
            self.min_rmse = None

        # Set logger level for verbose if wanted
        level = logger.level
        if self.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        # LOWESS screening, or RLM?
        screen = 'LOWESS' if self.screen_lowess else 'RLM'

        kwargs = dict(
            consecutive=self.consecutive,
            threshold=self.threshold,
            min_obs=self.min_obs,
            min_rmse=self.min_rmse,
            test_indices=self.test_indices,
            screening=screen,
            screening_crit=self.screen_crit,
            remove_noise=self.remove_noise,
            dynamic_rmse=self.dynamic_rmse,
            design_info=self.X.design_info,
            logger=logger
        )

        if self.reverse:
            self.yatsm_model = YATSM(np.flipud(self.X[clear, :]),
                                     np.fliplr(self.Y[:-1, clear]),
                                     **kwargs)
        else:
            self.yatsm_model = YATSM(self.X[clear, :],
                                     self.Y[:-1, clear],
                                     **kwargs)

        # Run
        self.yatsm_model.run()

        if self.commit_test:
            self.yatsm_model.record = self.yatsm_model.commission_test(
                self.commit_alpha)

        # List to store results
        if self.robust_results:
            self.result = self.yatsm_model.robust_record
        else:
            self.result = self.yatsm_model.record

        # Reset logger level
        logger.setLevel(level)

    def _retrieve_result_saved(self):
        """ Returns results opened from an existing model run
        """
        self.result = []
        # Set self.yatsm_model to None since we don't serialize the
        # reference to it
        self.yatsm_model = None

        record = self.results_pattern.replace('*', str(self._py)) + '.npz'
        record = os.path.join(self.location, self.results_folder, record)

        logger.info('Attempting to open: {f}'.format(f=record))

        if not os.path.isfile(record):
            logger.info('Could not find result for row {r}'.format(
                r=self._py))
            return

        z = np.load(record)
        if 'record' not in z.files:
            logger.error('Cannot find "record" file within saved result')
            return
        rec = z['record']

        result = np.where((rec['px'] == self._px) &
                          (rec['py'] == self._py))[0]

        logger.info(result)

        if result.size == 0:
            logger.info('Could not find result for column {px}'.format(
                px=self._px))
            return

        self.result = rec[result]

        # Set model specification from file
        if 'design' in z.files and 'design_matrix' in z.files:
            self.design = z['design']
            self.design_info = z['design_matrix']

    def get_prediction(self, band, usermx=None):
        """ Return the time series model fit predictions for any single pixel

        Arguments:
            band            time series band to predict
            usermx          optional - can specify ordinal dates as list

        Returns:
            [(mx, my)]      list of data points for time series fit where
                                length of list is equal to number of time
                                segments

        """
        if usermx is None:
            has_mx = False
        else:
            has_mx = True
        mx = []
        my = []

        design = re.sub(r'[\+\-][\ ]+C\(.*\)', '', self.design)
        coef_columns = []
        for k, v in self.design_info.column_name_indexes.iteritems():
            if not re.match('C\(.*\)', k):
                coef_columns.append(v)
        coef_columns = np.asarray(coef_columns)

        if len(self.result) > 0:
            logger.info('Plotting result')
            for rec in self.result:
                if band >= rec['coef'].shape[1]:
                    break

                ### Setup x values (dates)
                # Use user specified values, if possible
                if has_mx:
                    _mx = usermx[np.where((usermx >= rec['start']) &
                                 (usermx <= rec['end']))]
                    if len(_mx) == 0:
                        # User didn't ask for dates in this range
                        continue
                else:
                    # Check for reverse
                    if rec['end'] < rec['start']:
                        i_step = -1
                    else:
                        i_step = 1
                    _mx = np.arange(rec['start'],
                                    rec['end'],
                                    i_step)
                coef = rec['coef'][coef_columns, band]

                _mX = patsy.dmatrix(design, {'x': _mx}).T

                ### Calculate model predictions
                _my = np.dot(coef, _mX)

                ### Transform ordinal back into Python datetime
                _mx = [dt.fromordinal(int(m)) for m in _mx]
                ### Append
                mx.append(np.array(_mx))
                my.append(np.array(_my))

        return (mx, my)

    def get_breaks(self, band):
        """ Return an array of (x, y) data points for time series breaks """
        bx = []
        by = []
        # if len(self.result) > 1:
        #     for rec in self.result:
        #         if rec['break'] != 0:
        #             bx.append(dt.fromordinal(int(rec['break'])))
        #             index = [i for i, date in
        #                      enumerate(self.dates) if date == bx[-1]][0]
        #             if index < self._data.shape[1]:
        #                 by.append(self._data[band, index])
        for rec in self.result:
            if rec['break'] != 0:
                bx.append(dt.fromordinal(int(rec['break'])))
                index = [i for i, date in
                         enumerate(self.dates) if date == bx[-1]][0]
                if index < self._data.shape[1]:
                    by.append(self._data[band, index])

        return (bx, by)

    def retrieve_from_cache(self):
        """ Retrieve a timeseries pixel from cache

        Will attempt to read from an entire line of cached data, or from
        one single pixel of cached data.

        Returns:
          success (bool): True if read in from cache, False otherwise

        """
        cache_pixel = self.cache_name_lookup(self._px, self._py)
        cache_line = os.path.join(
            self.location, self.cache_folder,
            'yatsm_r{r}_n{n}_b{b}.npy.npz'.format(r=self._py,
                                                  n=self.length,
                                                  b=self.n_band))

        if self.read_cache and os.path.exists(cache_pixel):
            try:
                _read_data = np.load(cache_pixel)
            except:
                logger.error('Could not read from pixel cache file {f}'.format(
                    f=cache_pixel))
                pass
            else:
                # Test if newly read data is same size as current
                if _read_data.shape != self._data.shape:
                    logger.warning('Cached data may be out of date')
                    return False

                self._data = _read_data

                logger.info('Read in from single pixel cache')
                return True

        elif self.read_cache and os.path.exists(cache_line):
            try:
                _read_data = np.load(cache_line)['Y']
            except:
                logger.error('Could not read from line cache file {f}'.format(
                    f=cache_line))
            else:
                # Test if certain dimensions are compatible
                # self._data.shape ==> (n_band, length)
                # _read_data.shape ==> (n_band, length, ncol)
                if (self._data.shape[0] != _read_data.shape[0] or
                        self._data.shape[1] != _read_data.shape[1]):
                    logger.warning('Cached data may be out of date')
                    return False

                self._data = np.squeeze(_read_data[:, :, self._px])
                logger.info('Read in from entire line cache')
                return True

        return False

    def _find_metadata(self):
        """ Find image metadata for Landsat data """
        self.image_metadata = []

        if self.metadata_file_pattern:
            for fp in self.filepaths:
                fp_dir = os.path.dirname(fp)
                for fname in fnmatch.filter(os.listdir(fp_dir),
                        self.metadata_file_pattern):
                    self.image_metadata.append(os.path.join(fp_dir, fname))

        if not self.image_metadata:
            logger.warning('No image metadata found with pattern {p}'.format(
                p=self.metadata_file_pattern))

        if len(self.image_names) != len(self.image_metadata) \
                and self.image_metadata:
            raise Exception(
                'Inconsistent number of metadata files found '
                '({0} images vs {1} metadata files)'.format(
                    len(self.image_names), len(self.image_metadata)))


    def _get_metadata(self):
        """ Parse timeseries attributes for metadata """
        # ACCA ID
        self.cloud_cover = np.zeros((len(self.image_names)))
        if self.image_metadata:
            for i, mtl_file in enumerate(self.image_metadata):
                self.cloud_cover[i] = parse_landsat_MTL(
                    mtl_file, 'CLOUD_COVER')

        # Sensor ID
        self.sensor = np.array([n[0:3] for n in self.image_names])
        # Path/Row
        self.pathrow = np.array(['p{p}r{r}'.format(p=n[3:6], r=n[6:9])
                                for n in self.image_names])

        # Multitemporal noise screening - init to 0 (not screened)
        #   Will update this during model fitting
        self.multitemp_screened = np.ones(self.length)
        # Make an entry 0 so we get this in the unique values
        self.multitemp_screened[0] = 0

### OVERRIDEN "ADDITIONAL" OPTIONAL METHODS SUPPORTED BY CCDCTimeSeries
### INTERNAL SETUP METHODS
    def _check_yatsm(self):
        """ Check if YATSM is available """
        try:
            global YATSM
            global make_X
            global get_valid_mask
            global harm
            from ..yatsm.yatsm import YATSM
            from ..yatsm.utils import make_X
            from ..yatsm._cyprep import get_valid_mask
            from ..yatsm.regression.transforms import harm
        except:
            raise Exception('Could not import YATSM')
        else:
            self.has_results = True