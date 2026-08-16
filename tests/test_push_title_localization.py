"""L10n-Welle 15.08.: Titel-Templates der User-Content- und Inbound-Pushes.

Drei Zusicherungen, die die Push-Regressions-Klassen von früher ausschließen:
1. Deutsch bleibt BYTE-IDENTISCH zum bisherigen f-String-Titel.
2. Der Body wird NIE ersetzt (User-Text bzw. komponierter Inbound-Body) —
   das war der Florian/FO-Vorfall vom 11.08. mit der generischen Vorlage.
3. Alle App-Sprachen haben konkrete Titel; unbekannte Sprache fällt auf Deutsch.
"""
import app as m


def _copy(key, lang, args, title='ALT-TITEL', body='NUTZER-BODY',
          push_type='wall_comment'):
    data = {'type': push_type}
    field = ('title_localization_key'
             if push_type in m._PUSH_USER_CONTENT_TYPES else 'localization_key')
    data[field] = key
    data['localization_args'] = args
    return m._push_localize_system_copy(title, body, data, lang)


def test_alle_neuen_templates_registriert():
    for key in ('push_title_commented', 'push_title_replied',
                'push_title_replied_to_comment', 'push_title_replied_to_you',
                'push_title_mentioned', 'push_title_inbound_departed',
                'push_title_inbound_delay', 'push_title_inbound_arrived'):
        assert key in m._PUSH_SYSTEM_COPY, key
        assert set(m._PUSH_SYSTEM_COPY[key]) == set(m._PUSH_LANGUAGES), key
        # Body-Slot MUSS in JEDER Sprache None sein: User-Text/komponierter
        # Inbound-Body bleibt exakt erhalten.
        assert all(value[1] is None
                   for value in m._PUSH_SYSTEM_COPY[key].values()), key


def test_deutsch_bleibt_byteidentisch_zum_fstring():
    name = 'Tibor Quaas'
    title, body = _copy('push_title_commented', 'de', {'name': name})
    assert title == f'{name} hat kommentiert'
    assert body == 'NUTZER-BODY'

    flight = 'LH1551'
    title, body = _copy('push_title_inbound_departed', 'de',
                        {'flight': flight}, push_type='inbound_departure',
                        body='D-AIXP kommt als LH713 aus Singapur.')
    assert title == f'Dein Flieger ist gestartet · {flight}'
    assert body == 'D-AIXP kommt als LH713 aus Singapur.'


def test_body_wird_nie_ersetzt():
    for key, ptype in (('push_title_mentioned', 'forum_mention'),
                       ('push_title_inbound_arrived', 'inbound_arrival')):
        _, body = _copy(key, 'en', {'name': 'X', 'flight': 'LH1'},
                        body='ORIGINAL', push_type=ptype)
        assert body == 'ORIGINAL', key


def test_fremdsprachen_bleiben_konkret_und_nicht_deutsch():
    expected = {
        'en': 'Jenni replied',
        'it': 'Jenni ha risposto',
        'es': 'Jenni ha respondido',
        'fr': 'Jenni a répondu',
        'pt': 'Jenni respondeu',
    }
    for lang, wanted in expected.items():
        title, _ = _copy('push_title_replied', lang, {'name': 'Jenni'})
        assert title == wanted, lang


def test_unbekannte_sprache_faellt_weiter_auf_deutsch_zurueck():
    for lang in ('xx', ''):
        title, _ = _copy('push_title_replied', lang, {'name': 'Jenni'})
        assert title == 'Jenni hat geantwortet', lang


def test_fehlende_args_lassen_originaltitel_stehen():
    # Defensiv: kaputte/fehlende Args dürfen den Push nicht zerlegen.
    title, body = _copy('push_title_commented', 'de', {})
    assert title == 'ALT-TITEL'
    assert body == 'NUTZER-BODY'
