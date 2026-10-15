# Сторонние компоненты и лицензии

Misbot распространяется под MIT, автор — Stepan Ponamarev.
Полный текст находится в файле `LICENSE` в корне репозитория и установочного комплекта.
Лицензия Misbot относится к его собственным исходникам. Контейнерные образы,
системные пакеты и библиотеки сохраняют лицензии своих правообладателей;
MIT Misbot не заменяет их условия.

## Где искать сведения

- Образы серверных компонентов закреплены в [versions.json](versions.json).
  Лицензии и уведомления берите из соответствующих версий проектов PostgreSQL,
  Synapse, MAS, Element, LiveKit, Caddy и HAProxy и из самих образов.
- Состав JavaScript-зависимостей Matrix-ботов закреплён в lock-файле реферального
  бота: `bots/matrix-referral/package-lock.json` в репозитории,
  `referral-bot/package-lock.json` в полном установочном комплекте.
  Docker-сборка устанавливает эти зависимости; их уведомления остаются
  в пакетах внутри образа.
- Python-зависимости управляющей службы и Telegram-бота перечислены в их
  `requirements.txt`; часть пакетов установщик получает из репозиториев ОС.
  Для установленного системного пакета смотрите
  `/usr/share/doc/<имя-пакета>/copyright`.

Этот файл указывает источники сведений, а не заменяет полный набор уведомлений
из реально установленных пакетов и образов.

## Rich

Терминальный интерфейс использует [Rich](https://github.com/Textualize/rich)
под лицензией MIT. Rich устанавливается системным пакетом `python3-rich`;
его исходники и зависимости не включены в установочный архив Misbot.
Системные пакеты сохраняют собственные лицензии и уведомления,
доступные в `/usr/share/doc/<имя-пакета>/copyright`.

Ниже сохранён полный текст [лицензии Rich](https://github.com/Textualize/rich/blob/main/LICENSE).

```text
MIT License

Copyright (c) 2020 Will McGugan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
