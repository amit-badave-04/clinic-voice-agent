# Disclaimer

This repository is an **educational / portfolio demonstration** of a production-style
voice-AI booking system. It is **not a real clinic booking system**.

- Clinic, branch, practitioner and fee data were sourced from
  **Arogya Physiotherapy's public website**
  (https://arogyaphysiotherapy.com — team and contact pages), snapshotted on
  **16 July 2026**. Real public data is used deliberately: scheduling systems
  behave differently against realistic rosters, split-branch working hours, and
  genuine fee structures than against invented toy data.
- This project is **not affiliated with, endorsed by, or connected to
  Arogya Physiotherapy** in any way. The clinic's logo, branding, and phone
  numbers are deliberately not reproduced. The voice agent identifies itself as
  a demo assistant and cannot create real appointments at the actual clinic.
- Where the source lists locations beyond the two branches modeled here, some
  practitioner time blocks were mapped onto the modeled branches; these
  adaptations are documented in `seed/arogya_data.py`.
- All patient records in this system are **synthetic**. Do not enter real
  personal or medical information when testing; calls are recorded and
  transcribed by the underlying voice platform.
- Nothing in this repository constitutes medical advice.

If you represent Arogya Physiotherapy and would like the public data removed,
please open an issue and it will be replaced promptly.
