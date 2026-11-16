# Lucid diagrams

All nine report figures are drawn in Lucidchart. The PDF build uses `<name>.png` from this
folder. Sources: `<name>.lucid.json` (Lucid Standard Import) or `<name>.puml` (PlantUML).
Architecture updated 2026-11-16 for commit `0dd5083`; other figures last updated 2026-11-14.

| Name | Figure | Lucid title | Document ID | Edit URL | PNG saved |
|---|---|---|---|---|---|
| architecture | 1 | LanLink – System Architecture (CLI and GUI) | 6583b303-b1a4-4e53-9cdd-a64cad44f6fb | https://lucid.app/lucidchart/6583b303-b1a4-4e53-9cdd-a64cad44f6fb/edit | yes |
| frame-layout | 2 | LanLink - Figure 2 - Frame layout | 44a8f5ba-c19f-4937-9d1b-8e9e337b40e9 | https://lucid.app/lucidchart/44a8f5ba-c19f-4937-9d1b-8e9e337b40e9/edit | yes |
| state-connection | 3 | LanLink – Connection States | 3566111a-b2ff-4f64-91c0-24884605ad40 | https://lucid.app/lucidchart/3566111a-b2ff-4f64-91c0-24884605ad40/edit | yes |
| state-transfer | 4 | LanLink – File Transfer States | b40d4409-71fc-4f7d-a5a5-dc246c5eeb66 | https://lucid.app/lucidchart/b40d4409-71fc-4f7d-a5a5-dc246c5eeb66/edit | yes |
| seq-login | 5 | LanLink – Login Sequence | 876cb8dc-b407-471f-b734-ffa6bcd5dfa8 | https://lucid.app/lucidchart/876cb8dc-b407-471f-b734-ffa6bcd5dfa8/edit | yes |
| seq-chat | 6 | LanLink – Chat Message Lifecycle | 8885f930-47a5-4ac7-a085-8fa970c4805c | https://lucid.app/lucidchart/8885f930-47a5-4ac7-a085-8fa970c4805c/edit | yes |
| seq-file | 7 | LanLink – File Transfer | cc723b9f-f943-47a0-a028-ff1a13a2df43 | https://lucid.app/lucidchart/cc723b9f-f943-47a0-a028-ff1a13a2df43/edit | yes |
| seq-file-failures | 8 | LanLink – File Transfer Failures (1) | 3c70c380-e4d0-4ce8-9309-b290234a8d07 | https://lucid.app/lucidchart/3c70c380-e4d0-4ce8-9309-b290234a8d07/edit | yes |
| seq-file-failures-2 | 9 | LanLink – File Transfer Failures (2) | 6896283f-3c46-44a3-ac13-ac7c67a0ad37 | https://lucid.app/lucidchart/6896283f-3c46-44a3-ac13-ac7c67a0ad37/edit | yes |

## Architecture revision

Figure 1 was recreated from the saved native Lucid source because the original document
was inaccessible. It preserves the server and CLI layout and styles; Client B now shows
the Tk main thread, 100 ms event-queue polling, reader callbacks and temporary sender.
The native source and exported PNG above are current. The parent folder's architecture
`.mmd` and `.svg` are legacy drafts; Markdown and PDF Figure 1 use this Lucid PNG.
Sequence figures retain CLI-specific lifelines; their captions explain how the GUI uses
the same protocol exchanges.

## Superseded documents

Previous versions and earlier attempts, retained for reference:

| Lucid title | Document ID | Why |
|---|---|---|
| LanLink – System Architecture | 6502b25c-e5fb-4e46-b550-76c74ea00d1b | superseded by CLI/GUI architecture; original inaccessible |
| LanLink – Login Sequence | 6633d648-8f33-48a1-9168-83bb869a8253 | `/` doubled in a label |
| LanLink – Chat Message Lifecycle | df947b73-8bde-4605-b328-fb260ccffac5 | `/` doubled in notes |
| LanLink – File Transfer | 6aa6f020-b7ce-4401-bfe5-8c61fd4759d4 | `/` doubled in notes |
| LanLink – File Transfer | 5c1c73a4-feb3-4495-b179-bc2690bc7543 | wide participant boxes, text too small in print |
| LanLink – File Transfer Failures (1) | fe646927-31a2-4d08-8572-082ba9e7a693 | `/` doubled in a note |
| LanLink – slash test (delete me) | 585e0ca1-ae85-4394-9206-1ea26cbdcde4 | test document |
| LanLink – Connection States | 1fdccace-7b53-4a7c-83d1-2557166545da | Mermaid import, not exportable |
| LanLink – File Transfer States | c2e89c56-02b8-4afa-8140-41e19a7ec979 | Mermaid import, not exportable |
| LanLink – System Architecture | 94e5b3a6-4a98-411f-aa7b-95db4e841b81 | Mermaid import, not exportable |
| LanLink – System Architecture | 5c2ae854-42c1-430e-9d51-a07b11d1b458 | UDP label crossed a border |

Note: Lucid's sequence importer doubles a `/` inside notes and `alt` labels, so those
places use the division slash `∕` (U+2215) in the `.puml` sources.
