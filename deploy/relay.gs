/**
 * Google Apps Script — релей Telegram + Groq для бота «Рабочие задачи».
 *
 * Сервер в РФ заблокирован для api.telegram.org И для api.groq.com. Google их видит:
 *   - Telegram-методы: бот шлёт {secret, method, params} → скрипт зовёт метод Telegram.
 *   - Groq текст:      {secret, method:"groqChat", key, payload} → chat/completions.
 *   - Groq голос:      {secret, method:"groqVoice", key, model, file_path}
 *                      → скрипт качает файл из Telegram и шлёт в Groq Whisper.
 *
 * === Развернуть / обновить ===
 *  1. script.google.com → вставить этот код, вписать BOT_TOKEN и SECRET.
 *  2. «Развернуть» → «Веб-приложение»: Выполнять как «Я», Доступ «Все».
 *  3. При обновлении: «Развернуть» → «Управление развёртываниями» → карандаш →
 *     «Новая версия» (URL сохраняется).
 */

var BOT_TOKEN = 'PASTE_BOT_TOKEN';
var SECRET = 'PASTE_RELAY_SECRET';

function doPost(e) {
  var body = {};
  try { body = JSON.parse((e && e.postData && e.postData.contents) || '{}'); }
  catch (err) { return _json({ ok: false, error: 'bad json' }); }

  if (body.secret === SECRET) {
    // Groq: понимание текста
    if (body.method === 'groqChat') {
      var rc = UrlFetchApp.fetch('https://api.groq.com/openai/v1/chat/completions', {
        method: 'post', contentType: 'application/json', muteHttpExceptions: true,
        headers: { Authorization: 'Bearer ' + body.key },
        payload: JSON.stringify(body.payload || {})
      });
      return _json({ ok: rc.getResponseCode() === 200, status: rc.getResponseCode(), body: rc.getContentText() });
    }
    // Groq: распознавание голоса (скачать из Telegram + отправить в Whisper)
    if (body.method === 'groqVoice') {
      var fr = UrlFetchApp.fetch('https://api.telegram.org/file/bot' + BOT_TOKEN + '/' + body.file_path, { muteHttpExceptions: true });
      var blob = fr.getBlob().setName('voice.ogg');
      var rv = UrlFetchApp.fetch('https://api.groq.com/openai/v1/audio/transcriptions', {
        method: 'post', muteHttpExceptions: true,
        headers: { Authorization: 'Bearer ' + body.key },
        payload: { file: blob, model: body.model, language: 'ru' }
      });
      return _json({ ok: rv.getResponseCode() === 200, status: rv.getResponseCode(), body: rv.getContentText() });
    }
    // Любой метод Telegram
    if (body.method) {
      var rt = UrlFetchApp.fetch('https://api.telegram.org/bot' + BOT_TOKEN + '/' + body.method, {
        method: 'post', muteHttpExceptions: true, payload: body.params || {}
      });
      return ContentService.createTextOutput(rt.getContentText()).setMimeType(ContentService.MimeType.JSON);
    }
  }
  return _json({ ok: false, error: 'unknown request' });
}

function doGet(e) { return _json({ ok: true, info: 'task-bot relay alive' }); }
function _json(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }
