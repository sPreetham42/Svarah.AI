from app.services import agnes, campaign_brain, sarvam


def test_every_sarvam_language_is_wired_end_to_end():
    for name, code in sarvam.SUPPORTED_LANGUAGES.items():
        assert code in sarvam.TTS_LANGUAGE_CODES
        assert agnes.LOCALE_CODE_MAP[name] == code
        assert name in agnes.TARGETED_LANGUAGES and name in agnes.REGION_FOR_LANGUAGE
        if name != "English":
            assert name in campaign_brain._SCRIPT_RANGES
