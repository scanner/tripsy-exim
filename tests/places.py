#!/usr/bin/env python
#
"""
Real places, for tests that need a coordinate to mean something.

One value per place.  Three modules used to carry their own Narita, a
kilometre apart, and their own Vancouver, identical to the character --
so the same name meant slightly different points depending on which test
you were reading, and no test depended on the difference.

Airports go by their IATA code.  That is what an export calls them, what
`infer` matches on, and unambiguous in a way a city name is not: one
module had `SAN_FRANCISCO` holding SFO's runway coordinates, which is an
airport and not a city.

Precision is whatever the source publishes.  Nothing here wants
rounding: the distances tests turn on are kilometres apart, and the
tightest comparison in the suite -- one airport's own variants against
the next airport along -- has an order of magnitude of room either side.
"""

# Airports, by IATA code.
#
HND = (35.5494, 139.7798)
KIX = (34.43533, 135.243977)
NRT = (35.7719808, 140.3928501)
OAK = (37.7126, -122.2197)
SEA = (47.4502, -122.3088)
SFO = (37.6213, -122.3790)
SJC = (37.3639, -121.9289)
YVR = (49.1947, -123.1792)

# The three airports one metropolitan code covers, and the tightest real
# test of any guard that has to tell one airport from the next: SFO to
# OAK is 17km, OAK to SJC 47km, SFO to SJC 49km.  Every pair of them
# sits inside fifty kilometres.
#
# QSF is the San Francisco Bay Area in Sabre and ITA -- and Ain Arnat
# Airport in Setif, Algeria, to everyone else.  One code, two continents:
# the hazard `infer` refuses, in a single example.
#
QSF = (SFO, OAK, SJC)

# The two airports TYO covers, far enough apart to settle any argument
# about whether a metropolitan code names one place.
#
TYO = (NRT, HND)

# SFO as other sources publish it -- the centroid, and a runway rather
# than the terminal.  Around a kilometre and a half from `SFO`, which is
# the disagreement a guard has to absorb rather than refuse.
#
SFO_CENTROID = (37.6188, -122.3750)
SFO_RUNWAY = (37.615215, -122.389881)

# Places that are not airports.
#
KYOTO_STATION = (35.0116971, 135.7681616)
TOKYO = (35.6812, 139.7671)
TOKYO_STATION = (35.6762, 139.6503)

# What a geocoder answers for 'Kyoto Station' given no locality: a point
# in El Dorado County, California, about 8,900km from Kyoto.  It is the
# shape of a geocoder's failure -- confident, well-formed, and wrong.
#
KYOTO_STATION_IMPOSTOR = (38.69581586231335, -120.9094447761495)
