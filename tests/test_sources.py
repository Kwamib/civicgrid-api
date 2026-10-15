"""Fix-a-city source rule (app/sources.py), with the real Oct 15, 2026 cases.

12 of 378 manual saves cited another city's page or a non-government site
as the evidence for a "verified" leader. These would now need confirmation.
"""

import pytest

from app.sources import source_problem


@pytest.mark.parametrize(
    "source, city, state, city_url",
    [
        ("https://www.rose.net", "Thomasville", "GA", "https://www.rose.net"),
        (
            "https://www.cityofgaffney-sc.gov/184/City-Council",
            "Red Bank",
            "TN",
            "https://www.redbanktn.gov/",
        ),
        ("https://www.ci.gladstone.or.us", "Gladstone", "MI", "https://www.gladstonemi.gov"),
        ("https://www.homewoodal.net/", "Homewood", "IL", "https://www.homewoodil.gov"),
        ("https://www.cityofsalemnj.org/", "Salem", "OR", "https://www.cityofsalem.net"),
        ("https://www.kcmo.gov/mayor", "Kansas City", "MO", "https://wycokck.org"),
        ("not a url", "Anytown", "OH", None),
    ],
)
def test_needs_confirmation(source, city, state, city_url):
    assert source_problem(source, city, state, city_url)


@pytest.mark.parametrize(
    "source, city, state, city_url",
    [
        (
            "https://www.cityoffortwayne.in.gov/427/Mayors-Office",
            "Fort Wayne",
            "IN",
            "https://www.cityoffortwayne.org",
        ),
        ("https://mayor.lacity.gov/", "Los Angeles", "CA", "https://lacity.gov/"),
        ("https://www.ci.lubbock.tx.us", "Lubbock", "TX", None),
        ("https://www.stjohnsaz.gov/page/mayor-council", "St. Johns", "AZ", None),
        ("https://barnstable.gov/", "Barnstable Township", "MA", None),
        ("https://caguas.gov.pr/alcalde/", "Caguas", "PR", None),
        (
            "https://www.in.gov/cities/logansport/",
            "Logansport",
            "IN",
            "https://www.in.gov/cities/logansport/",
        ),
        ("www.redbanktn.gov/mayor", "Red Bank", "TN", None),
    ],
)
def test_city_own_site_is_accepted(source, city, state, city_url):
    assert source_problem(source, city, state, city_url) is None
