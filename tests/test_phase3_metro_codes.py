"""Phase 3 — City-/Metro-Code Resolver Tests.

Verifiziert:
- Alias-Map IATA_METRO_TO_BMF (CHI/ROM/STO/LON/NYC/...)
- _get_bmf_for_iata Source-Kaskade: IATA → Alias → KI → unresolved
- KI-Resolver wird gerufen wenn Alias fehlt
- KI ≥0.90 auto-resolve + audit
- KI 0.70-0.90 → review-pending
- KI <0.70 → unresolved
- BMF-Betrag IMMER aus Python+Tabelle (nie aus KI)
- Crew-Kontext im KI-Prompt
"""
import json
import os
import sys
import unittest
from unittest import mock

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('ANTHROPIC_API_KEY', 'sk-ant-test-dummy')
import app as app_module
from bmf_data import IATA_METRO_TO_BMF, IATA_TO_BMF, BMF_AUSLAND_BY_YEAR


def _clear_caches():
    app_module._ai_resolver_cache.clear()


# ─── Alias-Map deterministisch ──────────────────────────────────────────────

class TestMetroAliasMap(unittest.TestCase):
    """Phase 3 Alias-Map ohne KI-Call."""

    def setUp(self):
        _clear_caches()

    def test_city_code_chi_alias_maps_to_usa(self):
        self.assertIn('CHI', IATA_METRO_TO_BMF)
        self.assertIn('USA', IATA_METRO_TO_BMF['CHI'])
        self.assertIn('Chicago', IATA_METRO_TO_BMF['CHI'])

    def test_city_code_rom_alias_maps_to_italy(self):
        self.assertIn('ROM', IATA_METRO_TO_BMF)
        self.assertIn('Italien', IATA_METRO_TO_BMF['ROM'])
        self.assertIn('Rom', IATA_METRO_TO_BMF['ROM'])

    def test_city_code_sto_alias_maps_to_sweden(self):
        self.assertIn('STO', IATA_METRO_TO_BMF)
        self.assertEqual(IATA_METRO_TO_BMF['STO'], 'Schweden')

    def test_city_code_lon_resolves_via_iata_to_bmf(self):
        """LON ist bereits in IATA_TO_BMF (London) → primary lookup greift."""
        self.assertIn('LON', IATA_TO_BMF)
        self.assertIn('London', IATA_TO_BMF['LON'])
        # Plus: muss auch funktional resolven
        satz = app_module._get_bmf_for_iata('LON', 2025, _allow_ai_resolver=False)
        self.assertIsNotNone(satz)

    def test_city_code_nyc_resolves_via_iata_to_bmf(self):
        """NYC ist bereits in IATA_TO_BMF."""
        self.assertIn('NYC', IATA_TO_BMF)
        satz = app_module._get_bmf_for_iata('NYC', 2025, _allow_ai_resolver=False)
        self.assertIsNotNone(satz)

    def test_city_code_sel_resolves_via_iata_to_bmf(self):
        """SEL ist bereits in IATA_TO_BMF (Korea)."""
        self.assertIn('SEL', IATA_TO_BMF)
        satz = app_module._get_bmf_for_iata('SEL', 2025, _allow_ai_resolver=False)
        self.assertIsNotNone(satz)

    def test_metro_aliases_are_not_in_iata_to_bmf(self):
        """Metro-Aliase dürfen nicht in IATA_TO_BMF sein (sonst Phase 3 sinnlos)."""
        for code in IATA_METRO_TO_BMF.keys():
            self.assertNotIn(code, IATA_TO_BMF,
                f'{code} ist sowohl in IATA_TO_BMF als auch in IATA_METRO_TO_BMF')


# ─── _get_bmf_for_iata Source-Kaskade ───────────────────────────────────────

class TestSourceCascade(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_direct_iata_takes_precedence_over_alias(self):
        """ORD (direkt) hat Vorrang vor CHI (Metro-Alias)."""
        # ORD ist in IATA_TO_BMF
        satz_ord = app_module._get_bmf_for_iata('ORD', 2025, _allow_ai_resolver=False)
        self.assertIsNotNone(satz_ord)
        # _source nicht gesetzt = direct match
        self.assertNotIn('_source', satz_ord)

    def test_metro_alias_used_when_direct_missing(self):
        """CHI greift Alias-Layer + setzt _source='metro_alias'."""
        diag = {}
        satz = app_module._get_bmf_for_iata('CHI', 2025, _diag=diag,
                                             _allow_ai_resolver=False)
        self.assertIsNotNone(satz)
        self.assertEqual(satz.get('_source'), 'metro_alias')
        self.assertIn('Chicago', satz.get('_resolved_via', ''))
        # Diagnose-List enthält den Alias-Use
        self.assertIn('metro_alias_used', diag)
        self.assertEqual(len(diag['metro_alias_used']), 1)

    def test_metro_alias_chi_returns_chicago_satz(self):
        """CHI Metro → Chicago BMF-Sätze (sollten gleich USA-Chicago sein)."""
        satz_chi = app_module._get_bmf_for_iata('CHI', 2025, _allow_ai_resolver=False)
        satz_ord = app_module._get_bmf_for_iata('ORD', 2025, _allow_ai_resolver=False)
        # Beide sollten denselben BMF-Land referenzieren (Chicago)
        self.assertEqual(satz_chi.get('voll_24h'), satz_ord.get('voll_24h'))
        self.assertEqual(satz_chi.get('an_abreise'), satz_ord.get('an_abreise'))

    def test_metro_alias_rom_returns_italy_rome_satz(self):
        satz_rom = app_module._get_bmf_for_iata('ROM', 2025, _allow_ai_resolver=False)
        satz_fco = app_module._get_bmf_for_iata('FCO', 2025, _allow_ai_resolver=False)
        self.assertEqual(satz_rom.get('voll_24h'), satz_fco.get('voll_24h'))
        self.assertEqual(satz_rom.get('an_abreise'), satz_fco.get('an_abreise'))

    def test_metro_alias_sto_returns_sweden_satz(self):
        satz_sto = app_module._get_bmf_for_iata('STO', 2025, _allow_ai_resolver=False)
        satz_arn = app_module._get_bmf_for_iata('ARN', 2025, _allow_ai_resolver=False)
        self.assertEqual(satz_sto.get('voll_24h'), satz_arn.get('voll_24h'))

    def test_unknown_code_without_ai_returns_none(self):
        """Code nicht in IATA + nicht in Metro + KI deaktiviert → None + iata_unknown."""
        diag = {}
        satz = app_module._get_bmf_for_iata('ZZZ', 2025, _diag=diag,
                                             _allow_ai_resolver=False)
        self.assertIsNone(satz)
        self.assertIn('ZZZ', diag.get('iata_unknown', []))


# ─── KI-Resolver-Fallback ───────────────────────────────────────────────────

class TestAiFallback(unittest.TestCase):
    """KI-Resolver wird gerufen wenn direct + metro_alias fail."""

    def setUp(self):
        _clear_caches()

    def test_unknown_code_calls_ai_resolver_when_allowed(self):
        """Code unknown → KI-Resolver wird angerufen (mit place_code-kind)."""
        mock_result = {
            'resolved': True,
            'value': {'resolved_place': 'TestCity',
                      'country': 'Bulgarien',
                      'bmf_key': 'Bulgarien'},
            'confidence': 0.95,
            'reason': 'TestCity is in Bulgarien',
            'evidence': ['Crew-Kontext zeigt Bulgaria'],
            'needs_review': False,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_result) as mock_ai:
            diag = {}
            satz = app_module._get_bmf_for_iata('XYZUNK', 2025, _diag=diag,
                                                 _allow_ai_resolver=True,
                                                 _job_id='test')
            # KI wurde gerufen
            self.assertEqual(mock_ai.call_count, 1)
            call_kwargs = mock_ai.call_args
            self.assertEqual(call_kwargs.kwargs.get('kind'), 'place_code')
            self.assertEqual(call_kwargs.kwargs.get('uncertain_fact'), 'XYZUNK')

    def test_ai_high_confidence_resolves_to_bmf_satz(self):
        """KI conf=0.95 + bmf_key='Bulgarien' → Bulgaria-Sätze, _source='ai_resolver'."""
        mock_result = {
            'resolved': True,
            'value': {'bmf_key': 'Bulgarien'},
            'confidence': 0.95,
            'reason': '...', 'evidence': [],
            'needs_review': False,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_result):
            diag = {}
            satz = app_module._get_bmf_for_iata('XYZUNK', 2025, _diag=diag,
                                                 _job_id='test')
            self.assertIsNotNone(satz)
            self.assertEqual(satz.get('_source'), 'ai_resolver')
            self.assertEqual(satz.get('_resolved_via'), 'Bulgarien')
            # Audit-Entry
            self.assertIn('ai_resolver_used', diag)

    def test_ai_medium_confidence_creates_review_pending(self):
        """KI conf=0.80 → needs_review=True → ai_resolver_review_pending in diag."""
        mock_result = {
            'resolved': True,
            'value': {'bmf_key': 'Bulgarien'},
            'confidence': 0.80,
            'reason': 'wahrscheinlich Bulgarien', 'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_result):
            diag = {}
            satz = app_module._get_bmf_for_iata('XYZUNK', 2025, _diag=diag,
                                                 _job_id='test')
            # Bei needs_review=True: kein automatic resolve
            self.assertIsNone(satz)
            self.assertIn('ai_resolver_review_pending', diag)
            self.assertEqual(diag['ai_resolver_review_pending'][0]['suggestion'],
                             'Bulgarien')
            self.assertEqual(diag['ai_resolver_review_pending'][0]['confidence'], 0.80)

    def test_ai_low_confidence_returns_unresolved(self):
        """KI conf=0.50 → resolved=False → satz=None, iata_unknown."""
        mock_result = {
            'resolved': False,
            'value': {},
            'confidence': 0.50,
            'reason': 'unsicher', 'evidence': [],
            'needs_review': True,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_result):
            diag = {}
            satz = app_module._get_bmf_for_iata('XYZUNK', 2025, _diag=diag,
                                                 _job_id='test')
            self.assertIsNone(satz)
            # ai_resolver_review_pending bei <0.70 weil low-conf-Suggestion auch noted
            # (oder iata_unknown — beides ok)

    def test_no_ai_call_when_disabled(self):
        """_allow_ai_resolver=False → KI wird nie gerufen."""
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai') as mock_ai:
            app_module._get_bmf_for_iata('XYZUNK', 2025, _allow_ai_resolver=False)
            mock_ai.assert_not_called()


# ─── BMF-Betrag bleibt Python-Quelle ────────────────────────────────────────

class TestBmfAmountStaysPython(unittest.TestCase):

    def setUp(self):
        _clear_caches()

    def test_bmf_amount_always_from_python_table(self):
        """Auch wenn KI 'amount: 999' liefert (was sanitizer rejected), wäre Betrag
        immer aus BMF-Tabelle. Test: KI gibt 'amount' im value → sanitizer reject."""
        mock_result_with_amount = {
            'resolved': True,
            'value': {'bmf_key': 'Bulgarien', 'amount': 999.0},
            'confidence': 0.95,
            'reason': '', 'evidence': [],
            'needs_review': False,
        }
        # Phase 2 Sanitizer rejected dieses Result schon vor Phase 3 → wir prüfen
        # nur dass Phase 3 mit dem rejected Result umgeht (fallt-through zu None)
        # Aber für direkten Phase-3-Test: simuliere dass KI nur bmf_key gibt
        # (sanitizer würde 'amount' wegmachen). Dann sollte BMF-Betrag aus
        # BMF_AUSLAND_BY_YEAR kommen.
        mock_result_safe = {
            'resolved': True,
            'value': {'bmf_key': 'Bulgarien'},
            'confidence': 0.95,
            'reason': '', 'evidence': [],
            'needs_review': False,
        }
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                return_value=mock_result_safe):
            satz = app_module._get_bmf_for_iata('UNKWN', 2025, _job_id='t')
        self.assertIsNotNone(satz)
        # Betrag muss exakt aus BMF_AUSLAND_BY_YEAR[2025]['Bulgarien'] kommen
        from bmf_data import BMF_AUSLAND_BY_YEAR
        raw = BMF_AUSLAND_BY_YEAR[2025].get('Bulgarien')
        if raw:
            expected_v24, expected_aa = raw[0], raw[1]
            self.assertEqual(satz['voll_24h'], float(expected_v24))
            self.assertEqual(satz['an_abreise'], float(expected_aa))

    def test_inland_code_returns_none_no_ai_call(self):
        """Inland-Code (MUC) → None, kein KI-Call."""
        with mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai') as mock_ai:
            satz = app_module._get_bmf_for_iata('MUC', 2025)
            self.assertIsNone(satz)
            mock_ai.assert_not_called()


# ─── Crew-Kontext-Nachweis im Prompt (Integration) ──────────────────────────

class TestAiPromptCrewContextIntegration(unittest.TestCase):
    """Über _get_bmf_for_iata → _resolve_uncertain_fact_with_ai prüfen dass
    Prompt Airline-Crew-Kontext enthält."""

    def setUp(self):
        _clear_caches()

    def test_ai_prompt_via_bmf_fallback_contains_crew_context(self):
        captured_prompts = []
        original_build = app_module._ai_resolver_build_prompt
        def capture(kind, ctx, fact):
            p = original_build(kind, ctx, fact)
            captured_prompts.append(p)
            return p
        with mock.patch.object(app_module, '_ai_resolver_build_prompt',
                                side_effect=capture), \
             mock.patch.object(app_module, '_resolve_uncertain_fact_with_ai',
                                wraps=app_module._resolve_uncertain_fact_with_ai):
            # Mock anthropic client um keinen echten Call zu machen
            class _FakeC:
                def __init__(self): self.messages = self
                def create(self, **kw):
                    class _R:
                        content = [type('m', (), {'text': '{"resolved":false,"value":{},"confidence":0.1,"reason":"","evidence":[],"needs_review":true}'})]
                    return _R()
            # patch import path
            with mock.patch.dict('sys.modules', {'anthropic': mock.MagicMock(
                    Anthropic=lambda **kw: _FakeC())}):
                app_module._get_bmf_for_iata('ZZUNKQ', 2025, _job_id='ctx-test')
        self.assertTrue(captured_prompts, 'Prompt wurde nicht gebaut')
        p = captured_prompts[0]
        self.assertIn('Flugpersonal', p)
        self.assertIn('Crew', p)
        self.assertIn('Lufthansa', p)


if __name__ == '__main__':
    unittest.main(verbosity=2)
