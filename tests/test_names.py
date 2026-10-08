import pytest

from app.names import NameNeedsReview, clean_full_name, derive_last_name, same_person


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Mayor Nelson Andrews", "Nelson Andrews"),
        ("Sean Reed, Mayor", "Sean Reed"),
        ("Jessie Bellflowers - Mayor", "Jessie Bellflowers"),
        ("Mary Mendoza (Mayor)", "Mary Mendoza"),
        ('Mayor B.H. "Skip" Henderson III', 'B.H. "Skip" Henderson III'),
        ("Hon. Jane Doe", "Jane Doe"),
        ("Luis Mayorga", "Luis Mayorga"),
        ("Hon Lee", "Hon Lee"),
        ("  Muriel   Bowser ", "Muriel Bowser"),
    ],
)
def test_clean_full_name(raw, expected):
    assert clean_full_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Interim Mayor Jenkins", "Vice Mayor Ann Lee", "Mayor Pro Tem Jo Park", ". C. Grayson Day, Jr.", "Mayor", ""],
)
def test_clean_full_name_needs_review(raw):
    with pytest.raises(NameNeedsReview):
        clean_full_name(raw)


@pytest.mark.parametrize(
    "full, last",
    [
        ("Sam Hall III", "Hall"),
        ("James N. Giannettino, Jr.", "Giannettino"),
        ("Leonard Jones Jr", "Jones"),
        ("Clea McCaa II", "McCaa"),
        ("Sean Reed, Mayor", "Reed"),
        ("Mayor Nelson Andrews", "Andrews"),
        ("Ruder", "Ruder"),
    ],
)
def test_derive_last_name(full, last):
    assert derive_last_name(full) == last


def test_same_person():
    assert same_person("Muriel Bowser", "muriel  bowser")
    assert same_person("José García", "Jose Garcia")
    assert same_person("Mayor Doug Diny", "Doug Diny")
    assert not same_person("Jim Smith", "James Smith")
    assert not same_person("", "")
