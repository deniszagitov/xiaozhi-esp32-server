-- liquibase formatted sql

-- changeset DenisZagitov:202607201200
DELETE FROM `ai_model_provider`
WHERE `id` IN ('SYSTEM_ASR_OpenAIRealtime', 'SYSTEM_TTS_OpenAIStream');

INSERT INTO `ai_model_provider`
    (`id`, `model_type`, `provider_code`, `name`, `fields`, `sort`, `creator`, `create_date`, `updater`, `update_date`)
VALUES
    (
        'SYSTEM_ASR_OpenAIRealtime',
        'ASR',
        'openai_realtime',
        'OpenAI Realtime Transcription',
        '[{"key":"api_key","label":"API Key","type":"password"},{"key":"api_url","label":"WebSocket URL","type":"string"},{"key":"model","label":"Model","type":"string"},{"key":"language","label":"Language Hint","type":"string"},{"key":"delay","label":"Delay Level","type":"string"},{"key":"input_sample_rate","label":"Input Sample Rate","type":"number"},{"key":"output_dir","label":"Output Directory","type":"string"}]',
        15,
        1,
        NOW(),
        1,
        NOW()
    ),
    (
        'SYSTEM_TTS_OpenAIStream',
        'TTS',
        'openai_stream',
        'OpenAI Streaming Text-to-Speech',
        '[{"key":"api_key","label":"API Key","type":"password"},{"key":"api_url","label":"API URL","type":"string"},{"key":"model","label":"Model","type":"string"},{"key":"voice","label":"Voice","type":"string"},{"key":"instructions","label":"Voice Instructions","type":"string"},{"key":"speed","label":"Speed","type":"number"},{"key":"output_dir","label":"Output Directory","type":"string"}]',
        24,
        1,
        NOW(),
        1,
        NOW()
    );

DELETE FROM `ai_model_config`
WHERE `id` IN ('ASR_OpenAIRealtimeASR', 'TTS_OpenAIStreamTTS');

INSERT INTO `ai_model_config`
    (`id`, `model_type`, `model_code`, `model_name`, `is_default`, `is_enabled`, `config_json`, `doc_link`, `remark`, `sort`, `creator`, `create_date`, `updater`, `update_date`)
VALUES
    (
        'ASR_OpenAIRealtimeASR',
        'ASR',
        'OpenAIRealtimeASR',
        'OpenAI Realtime Transcription',
        0,
        1,
        '{"type":"openai_realtime","api_key":"","api_url":"wss://api.openai.com/v1/realtime?intent=transcription","model":"gpt-realtime-whisper","language":"","delay":"low","input_sample_rate":16000,"output_dir":"tmp/"}',
        'https://developers.openai.com/api/docs/guides/realtime-transcription',
        'Streams audio over WebSocket, uses local VAD to detect the end of speech, and commits the provider audio buffer. The adapter resamples server-side 16 kHz PCM to the 24 kHz PCM required by OpenAI Realtime.',
        15,
        NULL,
        NULL,
        NULL,
        NULL
    ),
    (
        'TTS_OpenAIStreamTTS',
        'TTS',
        'OpenAIStreamTTS',
        'OpenAI Streaming Text-to-Speech',
        0,
        1,
        '{"type":"openai_stream","api_key":"","api_url":"https://api.openai.com/v1/audio/speech","model":"gpt-4o-mini-tts","voice":"onyx","instructions":"","speed":1,"output_dir":"tmp/"}',
        'https://developers.openai.com/api/docs/guides/text-to-speech',
        'Consumes the OpenAI Speech API chunked HTTP response and encodes 24 kHz PCM to Opus as chunks arrive.',
        24,
        NULL,
        NULL,
        NULL,
        NULL
    );

DELETE FROM `ai_tts_voice` WHERE `tts_model_id` = 'TTS_OpenAIStreamTTS';
INSERT INTO `ai_tts_voice`
    (`id`, `tts_model_id`, `name`, `tts_voice`, `languages`, `voice_demo`, `remark`, `reference_audio`, `reference_text`, `sort`, `creator`, `create_date`, `updater`, `update_date`)
VALUES
    ('TTS_OpenAIStreamTTS0001', 'TTS_OpenAIStreamTTS', 'OpenAI Onyx', 'onyx', 'Multilingual', NULL, NULL, NULL, NULL, 1, NULL, NULL, NULL, NULL);

-- rollback DELETE FROM `ai_tts_voice` WHERE `tts_model_id` = 'TTS_OpenAIStreamTTS';
-- rollback DELETE FROM `ai_model_config` WHERE `id` IN ('ASR_OpenAIRealtimeASR', 'TTS_OpenAIStreamTTS');
-- rollback DELETE FROM `ai_model_provider` WHERE `id` IN ('SYSTEM_ASR_OpenAIRealtime', 'SYSTEM_TTS_OpenAIStream');
