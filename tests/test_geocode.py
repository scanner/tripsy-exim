#!/usr/bin/env python
#
"""
Test looking up the positions Tripsy will not work out for itself.

Nothing here reaches a geocoding service, and nothing here tests one.
Whether `geopy` works, or what Nominatim answers, is not this project's
to check.  What is ours is the call and what is done with the answer:
the obligations the usage policy puts on the caller, a cache that means
a place is asked about once, and refusing an answer that is somewhere
else entirely.
"""

# system imports
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 3rd party imports
import pytest
import pytest_check as check
from pytest_mock import MockerFixture

# Project imports
from tripsy_exim.geocode import (
    CACHE_ENV,
    MIN_DELAY_SECONDS,
    USER_AGENT,
    Cache,
    Found,
    default_cache,
    far_from,
    lookup_for,
    normalised,
    placed,
    plausible,
)

# Kyoto Station, and what a geocoder answers when asked for it without a
# locality: a point in El Dorado County, California.
#
# Full geocoder precision, because what is measured here is distance.
# `test_enrich` names some of the same places more roundly.
#
KYOTO = (35.0116971, 135.7681616)
IMPOSTOR = (38.69581586231335, -120.9094447761495)
NARITA = (35.7719808, 140.3928501)
SAN_FRANCISCO = (37.6152, -122.3899)


########################################################################
########################################################################
#
@dataclass(frozen=True)
class Answer:
    """
    The part of a `geopy` result this project reads.

    Named here rather than built from `geopy` so that a change in what
    the library returns shows up as a failing test rather than as a
    quietly different shape.
    """

    latitude: float
    longitude: float
    address: str
    raw: dict[str, str]


####################################################################
#
def counting(answers: dict[str, tuple[float, float] | None]) -> tuple:
    """A lookup answering from a dict, and the list of what it was asked."""
    asked: list[str] = []

    def look(address: str) -> Found:
        asked.append(address)
        return Found(address, answers.get(address), service="fake")

    return look, asked


########################################################################
########################################################################
#
class TestCache:
    """
    Tests for asking about a place once.

    'Results must be cached on your side.  Clients sending repeatedly the
    same query may be classified as faulty and blocked.'  Caching is a
    condition of use here, not an optimisation, which is why a lookup
    that found nothing is remembered too.
    """

    ####################################################################
    #
    @pytest.mark.parametrize(
        "wanted,answers,reloaded",
        [
            pytest.param(
                ["a", "a", "a"], {"a": (1.0, 2.0)}, False, id="within-one-run"
            ),
            pytest.param(
                ["a"], {"a": (1.0, 2.0)}, True, id="a-place-across-runs"
            ),
            pytest.param(["a"], {}, True, id="a-failure-across-runs"),
        ],
    )
    def test_the_service_is_asked_once(
        self,
        tmp_path: Path,
        wanted: list[str],
        answers: dict[str, tuple[float, float] | None],
        reloaded: bool,
    ) -> None:
        """
        GIVEN: an address wanted more than once, or wanted again later
        WHEN:  the addresses are resolved
        THEN:  the service is asked exactly once

        An address with no answer has no answer next week either, so a
        failure is remembered as carefully as a place.
        """
        path = tmp_path / "geocode.json"
        look, asked = counting(answers)
        cache = Cache(path)

        first = placed(wanted, look, cache)
        if reloaded:
            cache.save()
            first = placed(wanted, look, Cache(path))

        check.equal(asked, ["a"])
        check.equal(first["a"].placed, bool(answers))

    ####################################################################
    #
    def test_an_answer_is_remembered_whole(self, tmp_path: Path) -> None:
        """
        GIVEN: an answer saved with everything known about it
        WHEN:  a later run reads it back
        THEN:  its position and what it was matched as both survive

        A cached county reported as a clean answer would be worse than
        never having noticed it was a county.
        """
        path = tmp_path / "geocode.json"
        cache = Cache(path)
        cache.put(
            Found(
                "Elko, NV",
                (41.1958, -115.3273),
                label="Elko County, Nevada",
                kind="boundary:administrative",
            )
        )
        cache.save()

        again = Cache(path).get("Elko, NV")

        assert again is not None
        check.equal(again.position, (41.1958, -115.3273))
        check.equal(again.label, "Elko County, Nevada")
        check.is_true(again.coarse)

    ####################################################################
    #
    def test_forgetting_an_address_asks_again(self, tmp_path: Path) -> None:
        """
        GIVEN: an address whose cached answer was wrong
        WHEN:  it is forgotten and resolved again
        THEN:  the service is asked

        A wrong answer is not wrong in the cache's terms, so nothing
        would ever replace it on its own.
        """
        look, asked = counting({"Elko, NV": (40.8324, -115.7631)})
        cache = Cache(tmp_path / "geocode.json")
        cache.put(Found("Elko, NV", (41.1, -115.3)))

        check.is_true(cache.forget("Elko, NV"))
        check.is_false(cache.forget("never held"))
        placed(["Elko, NV"], look, cache)

        check.equal(asked, ["Elko, NV"])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "named,config,expected",
        [
            pytest.param(
                None,
                "/tmp/example-config",
                "/tmp/example-config/tripsy-exim/geocode.json",
                id="durable-storage-not-a-cache-directory",
            ),
            pytest.param(
                "/tmp/elsewhere.json",
                "/tmp/example-config",
                "/tmp/elsewhere.json",
                id="the-environment-moves-it",
            ),
        ],
    )
    def test_where_the_cache_lives(
        self,
        environment: MutableMapping[str, str],
        named: str | None,
        config: str,
        expected: str,
    ) -> None:
        """
        GIVEN: an environment naming a cache, or not
        WHEN:  the default is worked out
        THEN:  it is durable storage rather than a cache directory

        Keeping results is a condition of the terms, so the default is
        somewhere a disk cleaner will not empty.
        """
        environment["XDG_CONFIG_HOME"] = config
        environment.pop(CACHE_ENV, None)
        if named:
            environment[CACHE_ENV] = named

        assert default_cache() == Path(expected)


########################################################################
########################################################################
#
class TestPlausible:
    """Tests for refusing an answer that is somewhere else entirely."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "position,references,believed",
        [
            pytest.param(KYOTO, [NARITA], True, id="the-real-station"),
            pytest.param(
                IMPOSTOR, [NARITA], False, id="the-california-impostor"
            ),
            pytest.param(IMPOSTOR, [], True, id="nothing-to-measure-against"),
            pytest.param(
                (40.8324, -115.7631),
                [(37.4852, -122.2364)],
                True,
                id="a-long-day-of-driving",
            ),
            pytest.param(
                KYOTO,
                [SAN_FRANCISCO, NARITA],
                True,
                id="near-the-far-end-of-a-flight",
            ),
        ],
    )
    def test_a_result_is_measured_against_its_trip(
        self,
        position: tuple[float, float],
        references: list[tuple[float, float]],
        believed: bool,
    ) -> None:
        """
        GIVEN: a result and whatever its trip already places
        WHEN:  it is judged
        THEN:  one that could belong to the trip is believed

        A geocoder does not fail by answering nothing.  It fails by
        answering somewhere, and only the rest of the trip can say
        whether somewhere is credible.  The threshold has to clear a
        day's driving and a flight's far end while still refusing an
        answer on the wrong side of an ocean, and a trip that places
        nothing has to be believed or it could never be helped at all.
        """
        assert plausible(position, references) is believed
        if not references:
            check.is_none(far_from(position, references))


########################################################################
########################################################################
#
class TestCoarse:
    """Tests for noticing an answer to a larger question than was asked."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "kind,coarse",
        [
            pytest.param("boundary:administrative", True, id="a-county"),
            pytest.param("place:town", False, id="a-town"),
            pytest.param("place:house", False, id="a-house"),
            pytest.param("amenity:cafe", False, id="a-cafe"),
            pytest.param(None, False, id="the-service-said-nothing"),
        ],
    )
    def test_an_administrative_match_is_marked(
        self, kind: str | None, coarse: bool
    ) -> None:
        """
        GIVEN: a result the service classified
        WHEN:  it is asked whether it is coarse
        THEN:  an administrative boundary is, and a place is not

        'Elko, NV' answers with Elko County, 54 km from Elko.  That is
        not far enough to fail a distance check against its own trip, so
        the only thing that catches it is what the service called it.
        """
        found = Found("somewhere", (41.1958, -115.3273), kind=kind)

        assert found.coarse is coarse


########################################################################
########################################################################
#
class TestNormalised:
    """Tests for asking about an address in a form that can be answered."""

    ####################################################################
    #
    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param(
                "100 Example Avenue, Springfield, IL 62704 (555)123-4567",
                "100 Example Avenue, Springfield, IL 62704",
                id="a-phone-number-on-the-end",
            ),
            pytest.param(
                "200 EXAMPLE BOULEVARD, Springfield, IL, 627045299",
                "200 EXAMPLE BOULEVARD, Springfield, IL, 62704",
                id="a-zip-plus-four-with-no-hyphen",
            ),
            pytest.param(
                "100 Example Avenue , Springfield , Illinois , 62704-3292 , USA",
                "100 Example Avenue, Springfield, Illinois, 62704, USA",
                id="spaces-before-the-commas",
            ),
            pytest.param(
                "400 W Example Street\nSpringfield IL 62704\nUnited States",
                "400 W Example Street, Springfield IL 62704, United States",
                id="the-app-writes-newlines",
            ),
            pytest.param(
                "300 South Example Street, Springfield, IL, 62704",
                "300 South Example Street, Springfield, IL, 62704",
                id="already-clean",
            ),
        ],
    )
    def test_what_was_never_part_of_the_address_goes(
        self, raw: str, expected: str
    ) -> None:
        """
        GIVEN: an address as a booking or the app wrote it
        WHEN:  it is normalised
        THEN:  only what was never part of the address is removed

        Three of the first thirteen real lookups found nothing purely
        because of punctuation.
        """
        assert normalised(raw) == expected

    ####################################################################
    #
    def test_two_spellings_of_one_place_cost_one_request(
        self, tmp_path: Path
    ) -> None:
        """
        GIVEN: one hotel written two ways, one booking per traveller
        WHEN:  both are resolved
        THEN:  the service is asked once, and both get the answer

        The export writes a place once per traveller, punctuated as each
        booking happened to be.
        """
        clean = "300 South Example Street, Springfield, IL, 62704"
        look, asked = counting({clean: (34.0493, -118.2535)})

        found = placed(
            [clean, "300 South Example Street , Springfield , IL , 62704"],
            look,
            Cache(tmp_path / "geocode.json"),
        )

        check.equal(asked, [clean])
        check.equal(len(found), 2)
        check.is_true(
            all(f.position == (34.0493, -118.2535) for f in found.values())
        )

    ####################################################################
    #
    def test_a_cache_written_before_normalising_is_rekeyed(
        self, tmp_path: Path
    ) -> None:
        """
        GIVEN: a cache written before normalising, holding a found place,
               a failure whose form changes, and one whose form does not
        WHEN:  it is read back
        THEN:  only the failure worth retrying is dropped

        A place does not move, so its answer stands under the new key.
        A failure is exactly what normalising might fix -- but one that
        was already in normal form has been asked properly and answered,
        and asking again is what the terms warn against.
        """
        path = tmp_path / "geocode.json"
        first = Cache(path)
        first.put(Found("Somewhere , IL , 62704", (1.0, 2.0)))
        first.put(Found("200 EXAMPLE BOULEVARD, IL, 627045299", None))
        first.put(Found("Nowhere, IL", None))
        first.save()

        again = Cache(path)

        check.is_not_none(
            again.get("Somewhere, IL, 62704"), "a place does not move"
        )
        check.is_none(
            again.get("200 EXAMPLE BOULEVARD, IL, 62704"),
            "a failure whose form changed is worth asking again",
        )
        check.is_not_none(
            again.get("Nowhere, IL"),
            "a failure already in normal form stays remembered",
        )


########################################################################
########################################################################
#
class TestTheCallWeMake:
    """
    Tests for calling `geopy` the way its service requires.

    Not what the service answers, nor whether `geopy` works -- neither is
    this project's to test.  What is ours is the call, and Nominatim's
    usage policy puts its obligations on the caller.
    """

    ####################################################################
    #
    @pytest.fixture
    def recorded(self, mocker: MockerFixture) -> dict[str, Any]:
        """
        Stand in for `geopy`, recording the call and returning what a
        test puts in `answer`.

        Whether `geopy` can reach a service is not this project's to
        test.  What it hands back, and what is made of it, is.
        """
        seen: dict[str, Any] = {"asked": [], "answer": None}

        class Recorded:
            def __init__(self, **kwargs: Any) -> None:
                seen["built_with"] = kwargs

            def geocode(self, address: str) -> Any:
                seen["asked"].append(address)
                return seen["answer"]

        mocker.patch(
            "tripsy_exim.geocode.get_geocoder_for_service",
            return_value=Recorded,
        )
        return seen

    ####################################################################
    #
    def test_nominatim_is_called_as_its_policy_requires(
        self, recorded: dict[str, Any]
    ) -> None:
        """
        GIVEN: a lookup built against Nominatim
        WHEN:  the geocoder is built and asked about an address
        THEN:  the policy's obligations on the caller are all met

        'Provide a valid HTTP Referer or User-Agent identifying the
        application (stock User-Agents as set by http libraries will not
        do)', and 'no heavy uses (an absolute maximum of 1 request per
        second)'.  The agent names the repository rather than anyone's
        address, since it goes out in a header.
        """
        lookup_for("nominatim")("100 Example Avenue, Springfield, IL")

        check.equal(recorded["built_with"]["user_agent"], USER_AGENT)
        check.is_in("tripsy-exim/", USER_AGENT)
        check.is_in("github.com/scanner/tripsy-exim", USER_AGENT)
        check.is_not_in("@", USER_AGENT.split("+https://")[0])
        check.greater(MIN_DELAY_SECONDS, 1.0)
        check.equal(recorded["asked"], ["100 Example Avenue, Springfield, IL"])

    ####################################################################
    #
    def test_a_service_wanting_a_key_is_given_one_and_no_agent(
        self, recorded: dict[str, Any]
    ) -> None:
        """
        GIVEN: a lookup built against a service that authenticates
        WHEN:  the geocoder is built
        THEN:  the key reaches it, and Nominatim's agent does not

        The service is a choice that may change, which is the whole
        reason for going through `geopy`.
        """
        lookup_for("opencage", api_key="example-key")

        check.equal(recorded["built_with"]["api_key"], "example-key")
        check.is_not_in("user_agent", recorded["built_with"])

    ####################################################################
    #
    @pytest.mark.parametrize(
        "raw,kind,coarse",
        [
            pytest.param(
                {"class": "place", "type": "town"},
                "place:town",
                False,
                id="a-town",
            ),
            pytest.param(
                {"class": "boundary", "type": "administrative"},
                "boundary:administrative",
                True,
                id="the-county-instead-of-the-town",
            ),
            pytest.param({}, None, False, id="the-service-classified-nothing"),
        ],
    )
    def test_what_the_service_hands_back_is_read_whole(
        self,
        recorded: dict[str, Any],
        raw: dict[str, str],
        kind: str | None,
        coarse: bool,
    ) -> None:
        """
        GIVEN: an answer from the geocoder
        WHEN:  it is turned into a result
        THEN:  its position, label and classification all come across

        The classification is the part that matters and the part most
        easily dropped: 'Elko, NV' answers with a county 54 km from the
        town, close enough to pass any distance check, and the only
        thing that gives it away is what the service called it.
        """
        recorded["answer"] = Answer(
            latitude=41.1958,
            longitude=-115.3273,
            address="Elko County, Nevada, United States",
            raw=raw,
        )

        found = lookup_for("nominatim")("Elko, NV")

        check.equal(found.position, (41.1958, -115.3273))
        check.equal(found.label, "Elko County, Nevada, United States")
        check.equal(found.service, "nominatim")
        check.equal(found.kind, kind)
        check.equal(found.coarse, coarse)
        check.is_true(found.placed)

    ####################################################################
    #
    def test_an_answer_of_nothing_is_a_result_that_found_nothing(
        self, recorded: dict[str, Any]
    ) -> None:
        """
        GIVEN: a geocoder that found nothing
        WHEN:  the answer is read
        THEN:  a result comes back saying so, rather than an exception

        A miss is a fact to remember, not an error: not asking again is
        what the terms require.
        """
        recorded["answer"] = None

        found = lookup_for("nominatim")("Nowhere At All")

        check.is_false(found.placed)
        check.is_none(found.position)
        check.equal(found.address, "Nowhere At All")
