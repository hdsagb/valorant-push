# 第三方代码声明

本项目的扫码登录与国服商店接口实现复用了下列开源项目（MIT 许可证）的代码，文件位置：[`app/vendor/val_shop_ref.py`](app/vendor/val_shop_ref.py)。

## valorant-daily-shop-email

- 来源：https://github.com/Ceylan233/valorant-daily-shop-email
- 用途：QQ / 微信扫码登录流程、商店接口请求
- 许可证：MIT

```
MIT License

Copyright (c) 2024 Ceylan233

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

## 运行时依赖

以下依赖通过 `requirements.txt` 安装，各自遵循其原始许可证：

| 依赖 | 许可证 | 项目地址 |
| --- | --- | --- |
| aiohttp | Apache-2.0 | https://github.com/aio-libs/aiohttp |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| pystray | LGPL-3.0 | https://github.com/moses-palmer/pystray |
| tzdata | Apache-2.0 | https://github.com/python/tzdata |
