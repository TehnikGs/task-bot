/**
 * Google Apps Script — двусторонний релей Telegram для бота «Рабочие задачи».
 *
 * Зачем: сервер в РФ заблокирован для api.telegram.org. Google видит Telegram,
 * поэтому:
 *   - ОТПРАВКА: бот шлёт сюда {secret, method, params} → скрипт зовёт метод Telegram.
 *   - ПРИЁМ:    Telegram шлёт сюда апдейт (вебхук) → скрипт пересылает его на сервер.
 *   - ГОЛОС:    бот шлёт {secret, method:"downloadFile", file_path} → скрипт качает файл (base64).
 *
 * === Развернуть (один раз) ===
 *  1. https://script.google.com → «Новый проект», удалить шаблон, вставить этот файл.
 *  2. Впиши 4 значения ниже (BOT_TOKEN, SECRET, WEBHOOK_TARGET, WEBHOOK_SECRET).
 *  3. «Развернуть» → «Новое развёртывание» → «Веб-приложение»:
 *       - Выполнять от имени: Я
 *       - Доступ: Все (Anyone)
 *  4. Скопировать URL .../exec — это RELAY_URL для бота.
 *
 * ВАЖНО: при изменениях жать «Развернуть» → «Управление развёртываниями» →
 *        карандаш → «Новая версия» (URL сохранится). «Новое развёртывание» = НОВЫЙ URL.
 */

var BOT_TOKEN = 'PASTE_BOT_TOKEN';                    // токен от @BotFather
var SECRET = 'PASTE_RELAY_SECRET';                    // = RELAY_SECRET в .env бота
var WEBHOOK_TARGET = 'http://SERVER_IP:8081/webhook'; // публичный адрес сервера бота
var WEBHOOK_SECRET = 'PASTE_WEBHOOK_SECRET';          // = WEBHOOK_SECRET в .env бота

function doPost(e) {
  var body = {};
  try {
    body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
  } catch (err) {
    return _json({ ok: false, error: 'bad json' });
  }

  // 1) Запрос ОТ БОТА (secret + method) -> выполнить метод Telegram
  if (body.secret === SECRET && body.method) {
    if (body.method === 'downloadFile') {
      var fr = UrlFetchApp.fetch(
        'https://api.telegram.org/file/bot' + BOT_TOKEN + '/' + body.file_path,
        { muteHttpExceptions: true }
      );
      return _json({ ok: true, base64: Utilities.base64Encode(fr.getContent()) });
    }
    var resp = UrlFetchApp.fetch(
      'https://api.telegram.org/bot' + BOT_TOKEN + '/' + body.method,
      { method: 'post', muteHttpExceptions: true, payload: body.params || {} }
    );
    return ContentService
      .createTextOutput(resp.getContentText())
      .setMimeType(ContentService.MimeType.JSON);
  }

  // 2) Апдейт ОТ TELEGRAM (вебхук) -> переслать на сервер бота
  if (body.update_id !== undefined) {
    try {
      UrlFetchApp.fetch(WEBHOOK_TARGET, {
        method: 'post',
        contentType: 'application/json',
        muteHttpExceptions: true,
        headers: { 'X-Relay-Secret': WEBHOOK_SECRET },
        payload: JSON.stringify(body)
      });
    } catch (err) {}
    return _json({ ok: true });
  }

  return _json({ ok: false, error: 'unknown request' });
}

function doGet(e) {
  return _json({ ok: true, info: 'task-bot relay alive' });
}

function _json(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
