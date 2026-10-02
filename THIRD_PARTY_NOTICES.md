# Third-party sources

The voice split result/items structure in splitter.py is adapted from Steven Meers' Second Brain, backend/ingestion/handlers/voice.py, revision b7b9b0612a769013ca970cd29701b995ed06f1f7.
https://github.com/sdmeers/second_brain

MIT License

Copyright (c) 2026 Steven Meers

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

Other packages are installed independently from their official distributions.
XiaomiAlbumSyncer 0.19.1 is used as an independent GPL-3.0 executable, not embedded.
Prefect is Apache-2.0; filelock and markdown-it-py are MIT.

Codex execution uses OpenAI's official openai-codex 0.159.0 Python SDK
(Apache-2.0) without modifications, connected to the tested desktop CLI runtime.

The independent remnote-plugin uses the official @remnote/plugin-sdk 0.0.46.
Its native/sandbox entry loading pattern is adapted from the official plugin
template and robert7/remnote-mcp-bridge 0.17.0 webpack configuration
(revision 2479be3801db3ec002bf0dad9eeccb8eba3c5fba), MIT License.
Copyright (c) 2023-2026 RemNote, Quentin Tousart, Robert Spiegel.
The MIT permission and warranty text above applies to this adapted pattern.
No MCP service, API, running plugin or source checkout is a runtime dependency.
