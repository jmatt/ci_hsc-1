__all__ = ["RawValidation", "DetrendValidation", "SfmValidation", "SkymapValidation", "WarpValidation",
           "CoaddValidation", "DetectionValidation", "MergeDetectionsValidation", "MeasureValidation",
           "MergeMeasurementsValidation", "ForcedValidation",]

import os
import numpy
import argparse
from lsst.base import setNumThreads
from lsst.pex.logging import getDefaultLog
from lsst.daf.persistence import Butler
from lsst.meas.astrom import LoadAstrometryNetObjectsTask


class IdValueAction(argparse.Action):
    """argparse action callback to process a data ID

    We don't support as full a range of operators as does the pipe_base ArgumentParser
    (e.g., '^' to join multiple values, and the '..' for a range are NOT supported).
    We're just stuffing "key=value" pairs into a list of dicts.
    """
    def __call__(self, parser, namespace, values, option_string):
        result = {}
        for nameValue in values:
            key, _, value = nameValue.partition("=")
            if key in result:
                parser.error("%s appears multiple times in %s" % (key, option_string))
            result[key] = value
        argName = option_string.lstrip("-")
        getattr(namespace, argName).append(result)


def main():
    setNumThreads(0)  # We're being run in parallel
    parser = argparse.ArgumentParser()
    parser.add_argument("cls", help="Name of validation class")
    parser.add_argument("root", help="Data repository root")
    parser.add_argument("--rerun", default=None, help="Rerun name")
    parser.add_argument("--id", nargs="*", action=IdValueAction, default=[],
                        help="Data identifier, e.g., visit=123 ccd=45", metavar="KEY=VALUE")
    args = parser.parse_args()

    if not args.cls.endswith("Validation") or args.cls not in globals():
        parser.error("Unrecognised validation class: %s" % (args.cls))

    root = args.root
    if args.rerun:
        root = os.path.join(root, "rerun", args.rerun)

    validator = globals()[args.cls](root)
    for dataId in args.id:
        dataId = {key: int(value) if key in ("visit", "ccd", "tract") else value for
                  key, value in dataId.iteritems()}
        validator.run(dataId)


class Validation(object):
    _datasets = [] # List of datasets to check we can read
    _files = [] # List of datasets to check that file exists
    _sourceDataset = None # Dataset name of source catalog
    _minSources = 100 # Minimum number of sources
    _matchDataset = None # Dataset name of matches
    _minMatches = 10 # Minimum number of matches
    _butler = {}

    def __init__(self, root, log=None):
        if log is None:
            log = getDefaultLog()
        self.log = log
        self.root = root
        self._butler = None

    @property
    def butler(self):
        if not self._butler:
            self._butler = Butler(self.root)
        return self._butler

    def assertTrue(self, description, success):
        logger = self.log.info if success else self.log.fatal
        logger("%s: %s" % (description, "PASS" if success else "FAIL"))
        if not success:
            raise AssertionError("Failed test: %s" % description)

    def assertFalse(self, description, success):
        self.assertTrue(description, not success)

    def assertEqual(self, description, obj1, obj2):
        self.assertTrue(description + " (%s = %s)" % (obj1, obj2), obj1 == obj2)

    def assertGreater(self, description, num1, num2):
        self.assertTrue(description + " (%d > %d)" % (num1, num2), num1 > num2)

    def assertLess(self, description, num1, num2):
        self.assertTrue(description + " (%d < %d)" % (num1, num2), num1 < num2)

    def assertGreaterEqual(self, description, num1, num2):
        self.assertTrue(description + " (%d >= %d)" % (num1, num2), num1 >= num2)

    def assertLessEqual(self, description, num1, num2):
        self.assertTrue(description + " (%d <= %d)" % (num1, num2), num1 <= num2)


    def checkApertureCorrections(self, catalog):
        """Utility function for derived classes that want to verify that aperture corrections were applied
        """
        for alg in ("base_PsfFlux", "base_GaussianFlux"):
            self.assertTrue("Aperture correction fields for %s are present." % alg,
                            (("%s_apCorr" % alg) in catalog.schema) and
                            (("%s_apCorrSigma" % alg) in catalog.schema) and
                            (("%s_flag_apCorr" % alg) in catalog.schema))


    def validateDataset(self, dataId, dataset):
        self.assertTrue("%s exists" % dataset, self.butler.datasetExists(datasetType=dataset, dataId=dataId))
        # Just warn if we can't load a PropertySet or PropertyList; there's a known issue
        # (DM-4927) that prevents these from being loaded on Linux, with no imminent resolution.
        try:
            data = self.butler.get(dataset, dataId)
            self.assertTrue("%s readable (%s)" % (dataset, data.__class__), data is not None)
        except:
            if dataset.endswith("metadata"):
                self.log.warn("Unable to load '%s'; this is likely DM-4927." % dataset)
                return
            raise

    def validateFile(self, dataId, dataset):
        filename = self.butler.get(dataset + "_filename", dataId)[0]
        self.assertTrue("%s exists on disk" % dataset, os.path.exists(filename))
        self.assertGreater("%s has non-zero size" % dataset, os.stat(filename).st_size, 0)

    def validateSources(self, dataId):
        src = self.butler.get(self._sourceDataset, dataId)
        self.assertGreater("Number of sources", len(src), self._minSources)
        return src

    def validateMatches(self, dataId):
        sources = self.butler.get(self._sourceDataset, dataId)
        packedMatches = self.butler.get(self._matchDataset, dataId)
        refObjLoaderConfig = LoadAstrometryNetObjectsTask.ConfigClass()
        refObjLoader = LoadAstrometryNetObjectsTask(refObjLoaderConfig)
        matches = refObjLoader.joinMatchListWithCatalog(packedMatches, sources)
        self.assertGreater("Number of matches", len(matches), self._minMatches)

    def run(self, dataId, **kwargs):
        if kwargs:
            dataId = dataId.copy()
            dataId.update(kwargs)

        for ds in self._datasets:
            self.log.info("Validating dataset %s for %s" % (ds, dataId))
            self.validateDataset(dataId, ds)

        for f in self._files:
            self.log.info("Validating file %s for %s" % (f, dataId))
            self.validateFile(dataId, f)

        if self._sourceDataset is not None:
            self.log.info("Validating source output for %s" % dataId)
            self.validateSources(dataId)

        if self._matchDataset is not None:
            self.log.info("Validating matches output for %s" % dataId)
            self.validateMatches(dataId)

    def scons(self, *args, **kwargs):
        """Strip target,source,env from scons' call"""
        kwargs.pop("target")
        kwargs.pop("source")
        kwargs.pop("env")
        return self.run(*args, **kwargs)


class RawValidation(Validation):
    _datasets = ["raw"]

class DetrendValidation(Validation):
    _datasets = ["bias", "dark", "flat"]

class SfmValidation(Validation):
    _datasets = ["processCcd_config", "processCcd_metadata", "calexp", "calexpBackground",
                 "icSrc", "icSrc_schema", "src_schema"]
    _sourceDataset = "src"
    _matchDataset = "srcMatch"

    def validateSources(self, dataId):
        catalog = Validation.validateSources(self, dataId)
        self.checkApertureCorrections(catalog)
        # Check that at least 95% of the stars we used to model the PSF end up classified as stars. We 
        # certainly need much more purity than that to build good PSF models, but
        # this should verify that aperture correction and extendendess are running and configured reasonably
        # (but it may not be sensitive enough to detect subtle bugs).
        psfStars = catalog.get("calib_psfUsed")
        extStars = catalog.get("base_ClassificationExtendedness_value") < 0.5
        # changing from 95 to 85 until DM-6925 is complete
        self.assertGreater(
            "At least 85% of sources used to build the PSF are classified as stars",
            numpy.logical_and(extStars, psfStars).sum(),
            0.85*psfStars.sum()
        )

class SkymapValidation(Validation):
    _datasets = ["deepCoadd_skyMap"]

class WarpValidation(Validation):
    _datasets = ["deepCoadd_tempExp", "deep_makeCoaddTempExp_config", "deep_makeCoaddTempExp_metadata"]

class CoaddValidation(Validation):
    _datasets = ["deepCoadd", "deep_safeClipAssembleCoadd_config", "deep_safeClipAssembleCoadd_metadata"]

class DetectionValidation(Validation):
    _datasets = ["deepCoadd_det_schema", "detectCoaddSources_config", "detectCoaddSources_metadata"]
    _sourceDataset = "deepCoadd_det"

class MergeDetectionsValidation(Validation):
    _datasets = ["mergeCoaddDetections_config", "deepCoadd_mergeDet_schema"]
    _sourceDataset = "deepCoadd_mergeDet"

class MeasureValidation(Validation):
    _datasets = ["measureCoaddSources_config", "measureCoaddSources_metadata", "deepCoadd_meas_schema"]
    _sourceDataset = "deepCoadd_meas"
    _matchDataset = "deepCoadd_srcMatch"

    def validateSources(self, dataId):
        catalog = Validation.validateSources(self, dataId)
        self.assertTrue("calib_psfCandidate field exists in deepCoadd_meas catalog",
                        "calib_psfCandidate" in catalog.schema)
        self.assertTrue("calib_psfUsed field exists in deepCoadd_meas catalog",
                        "calib_psfUsed" in catalog.schema)
        self.checkApertureCorrections(catalog)
        # Check that at least 95% of the stars we used to model the PSF end up classified as stars
        # on the coadd.  We certainly need much more purity than that to build good PSF models, but
        # this should verify that flag propagation, aperture correction, and extendendess are all
        # running and configured reasonably (but it may not be sensitive enough to detect subtle
        # bugs).
        psfStars = catalog.get("calib_psfUsed")
        extStars = catalog.get("base_ClassificationExtendedness_value") < 0.5
        # changing from 95 to 85 until DM-6925 is complete
        self.assertGreater(
            "Less than 85% of sources used to build the PSF are classified as stars on the coadd",
            numpy.logical_and(extStars, psfStars).sum(),
            0.85*psfStars.sum()
        )

class MergeMeasurementsValidation(Validation):
    _datasets = ["mergeCoaddMeasurements_config", "deepCoadd_ref_schema"]
    _sourceDataset = "deepCoadd_ref"

class ForcedValidation(Validation):
    _datasets = ["deepCoadd_forced_src_schema", "deepCoadd_forced_config", "deepCoadd_forced_metadata"]
    _sourceDataset = "deepCoadd_forced_src"

    def validateSources(self, dataId):
        catalog = Validation.validateSources(self, dataId)
        self.checkApertureCorrections(catalog)
