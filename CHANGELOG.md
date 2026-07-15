# Changelog

## 1.2.0 (2026-07-15)

### Added
- Reply-to threading: context from replied messages (msg.link.message.text)
- Inline feedback buttons (👍/👎) via FEEDBACK: directive + message_callback handling
- `pause_typing_for_chat()` — support for approval workflow
- `resume_typing_for_chat()` — stub for gateway compatibility
- `set_topic_recovery_fn()` — stub for gateway compatibility
- `set_authorization_check()` — stub for gateway compatibility
- `_busy_text_mode` attribute initialization

### Fixed
- `edit_message()` returns `_SendResult` instead of `True` — fixes `Progress message error: 'bool' object has no attribute 'success'`
- `connect()` accepts `**kwargs` for `is_reconnect` parameter from gateway
- `validate_config()` return type inconsistency (now properly returns `bool`)
- SSL context for `platform-api2.max.ru` via Russian Trusted Root CA
- Webhook registration includes `message_callback` update type

### Changed
- API base URL: `platform-api.max.ru` → `platform-api2.max.ru`
- `_standalone_send()` signature: added `platform_config`, `thread_id`, `media_files`, `force_document` params
- `send()` method returns `_SendResult` instead of plain dict

## 1.0.0 (2026-07-07)

### Added
- Initial release: text messages, media uploads, webhook mode
- STT support — download audio from Max and pass to Hermes transcription pipeline
- SSL support for Russian Trusted Root CA
