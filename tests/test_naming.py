# -*- coding: utf-8 -*-
from uploader_360foryou.naming import ascii_slug, remote_filename


def test_plain_name_passes_through():
    assert ascii_slug('Buildings_2024') == 'Buildings_2024'


def test_spaces_and_specials_collapse_to_underscore():
    assert ascii_slug('My layer (final)!') == 'My_layer_final'


def test_diacritics_are_transliterated():
    assert ascii_slug('Straße Café') == 'Strae_Cafe'  # NFKD keeps base letters


def test_cyrillic_only_falls_back():
    assert ascii_slug('Здания') == 'layer'
    assert ascii_slug('Здания', fallback='vector') == 'vector'


def test_edge_dots_and_dashes_stripped():
    assert ascii_slug('.hidden.') == 'hidden'
    assert ascii_slug('...') == 'layer'
    assert ascii_slug('') == 'layer'
    assert ascii_slug(None) == 'layer'


def test_long_names_truncated():
    assert len(ascii_slug('x' * 500)) == 100


def test_remote_filename_dedupes():
    taken = set()
    assert remote_filename('Здания', 'kml', taken) == 'layer.kml'
    assert remote_filename('Дороги', 'kml', taken) == 'layer_2.kml'
    assert remote_filename('layer', 'KML', taken) == 'layer_3.kml'
    assert remote_filename('other', '.tif', taken) == 'other.tif'
    assert taken == {'layer.kml', 'layer_2.kml', 'layer_3.kml', 'other.tif'}


def test_remote_filename_no_extension():
    taken = set()
    assert remote_filename('readme', '', taken) == 'readme'
    assert remote_filename('readme', None, taken) == 'readme_2'
