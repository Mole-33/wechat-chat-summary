# Third-party notices

## wechatauto-replica

The WeChat 4.x database compatibility layer includes `vendor/wechat4_db.py`,
derived from `wechatauto-replica` 1.2.2.3:

- Source: https://github.com/fanyuantaier/wechatauto-replica
- License: Apache License 2.0

The upstream license is preserved in `vendor/LICENSE.wechatauto-replica`.
Local changes disable persistent key caches and place decrypted working copies
inside an application-owned temporary session directory.

