"""
Model class for MyTardis API v1's InstrumentResource.
See: https://github.com/mytardis/mytardis/blob/3.7/tardis/tardis_portal/api.py
"""

import json
import urllib
import requests

from ..settings import SETTINGS
from ..logs import logger
from ..utils.exceptions import DoesNotExist
from ..utils.exceptions import Unauthorized
from ..utils.exceptions import DuplicateKey
from .facility import FacilityModel
from . import HandleHttpError


class InstrumentModel(object):
    """
    Model class for MyTardis API v1's InstrumentResource.
    """
    def __init__(self, name, instrumentJson):
        self.name = name
        self.json = instrumentJson
        self.instrumentId = instrumentJson['id']
        self.facility = FacilityModel(facilityJson=instrumentJson['facility'])

    @property
    def resourceUri(self):
        """
        Return the API resource URI..
        """
        return self.json['resource_uri']

    @staticmethod
    def CreateInstrument(facility, name):
        """
        Create instrument.
        """
        url = "%s/api/v1/instrument/" % SETTINGS.general.myTardisUrl
        instrumentJson = {
            "facility": facility.resourceUri,
            "name": name}
        data = json.dumps(instrumentJson)
        headers = SETTINGS.defaultHeaders
        response = requests.post(headers=headers, url=url, data=data)
        if response.status_code == 201:
            instrumentJson = response.json()
            return InstrumentModel(name=name, instrumentJson=instrumentJson)
        else:
            if response.status_code == 401:
                message = "Couldn't create instrument \"%s\" " \
                          "in facility \"%s\"." % (name, facility.name)
                message += "\n\n"
                message += "Please ask your MyTardis administrator to " \
                           "check the permissions of the \"%s\" " \
                           "user account." % SETTINGS.general.username
                raise Unauthorized(message)
            HandleHttpError(response)

    @staticmethod
    def GetInstrument(facility, name):
        """
        Get instrument.
        """
        url = "%s/api/v1/instrument/?format=json&facility__id=%s&name=%s" \
            % (SETTINGS.general.myTardisUrl, facility.facilityId,
               urllib.quote(name.encode('utf-8')))
        response = requests.get(url=url, headers=SETTINGS.defaultHeaders)
        if response.status_code != 200:
            HandleHttpError(response)
        instrumentsJson = response.json()
        numInstrumentsFound = \
            instrumentsJson['meta']['total_count']
        if numInstrumentsFound == 0:
            message = "Instrument \"%s\" was not found in MyTardis" % name
            logger.warning(message)
            raise DoesNotExist(message, response, modelClass=InstrumentModel)
        else:
            logger.debug("Found instrument record for name \"%s\" "
                         "in facility \"%s\"" % (name, facility.name))
            instrumentJson = instrumentsJson['objects'][0]
            return InstrumentModel(name=name, instrumentJson=instrumentJson)

    @staticmethod
    def RenameInstrument(facilityName, oldInstrumentName, newInstrumentName):
        """
        Rename the instrument
        """
        facilities = FacilityModel.GetMyFacilities()
        facility = None
        for facil in facilities:
            if facilityName == facil.name:
                facility = facil
                break
        if facility is None:
            raise Exception("Facility is None in "
                            "SettingsModel's RenameInstrument.")
        try:
            oldInstrument = \
                InstrumentModel.GetInstrument(facility, oldInstrumentName)
        except DoesNotExist:
            raise Exception("Instrument record for old instrument "
                            "name not found in SettingsModel's "
                            "RenameInstrument.")
        try:
            _ = InstrumentModel.GetInstrument(facility, newInstrumentName)
            raise DuplicateKey("Instrument with name \"%s\" "
                               "already exists" % newInstrumentName)
        except DoesNotExist:
            oldInstrument.Rename(newInstrumentName)

    def Rename(self, name):
        """
        Rename instrument.
        """
        logger.info("Renaming instrument \"%s\" to \"%s\"."
                    % (str(self), name))
        url = "%s/api/v1/instrument/%d/" \
            % (SETTINGS.general.myTardisUrl, self.instrumentId)
        uploaderJson = {"name": name}
        data = json.dumps(uploaderJson)
        headers = SETTINGS.defaultHeaders
        response = requests.put(headers=headers, url=url, data=data)
        if response.status_code == 200:
            logger.info("Renaming instrument succeeded.")
        else:
            HandleHttpError(response)
