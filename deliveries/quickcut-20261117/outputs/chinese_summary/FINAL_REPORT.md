# 中文概括视频交付报告

- 视频：/Users/tangbao/Documents/ChatGPT/CV Project/outputs/chinese_summary/final_chinese_summary.mp4
- 时间线：/Users/tangbao/Documents/ChatGPT/CV Project/outputs/chinese_summary/final_chinese_summary.json
- 字幕：/Users/tangbao/Documents/ChatGPT/CV Project/outputs/chinese_summary/final_chinese_summary.srt
- 接触表：/Users/tangbao/Documents/ChatGPT/CV Project/outputs/chinese_summary/final_chinese_summary_contact.jpg
- 全部推理及配音：本地 Qwen2.5-VL 3B、Whisper、macOS say。
- 男声：Reed (中文（中国大陆）)，rate 145；根据逐句 WAV 实测时长适配。已实际合成比较 Reed / Rocko / Grandpa，采用声学和 ASR 指标，不宣称人工试听。
- 新索引：212 个真实关键帧，原视频 Whisper ASR 已执行；约495秒进入片尾，本片全部源片段止于493.5秒。
- Qwen：从连续378–493.5秒候选窗口分三幕实际选择20个源片段，并通过本地模型调用自行修正排列；最终源范围381–493.5秒。
- 三幕：0–18秒林间对峙（6段）；18–42秒滑翔遇险（8段）；42–58.5秒抓住对手、风筝揭晓与笑容收束（6段）。
- 编码：H.264，1280×720，60 fps；AAC，48 kHz，双声道。
- 视频/音频/容器时长：58.500 / 58.500 / 58.500秒。
- 视频/音频首解码帧PTS及stream start_time：均为0。
- Whisper small：成片末句“终于笑了”逐词结束于58.24秒，距离结尾0.26秒；未把尾部幻觉字幕算作旁白。
- 原声保留：逐样本回归测得增益0.298345，扣除旁白后相关0.952908，混音重建相关0.997924，通过。
- 字幕：11条，逐句一致，无重叠或零时长，跨镜头连续；结束58.5秒。
- 完整解码：FFmpeg xerror/explode，无错误。
- 抽帧检查：20段均已查看；故事连贯，无片尾混入，旁白字幕对应动作。
- 测试：82 passed in 3.27s；完整smoke demo通过。
- Git commit：6cb978d7e34b7d57f8d1ba8679d7f310052d55c1；git push origin HEAD:main 成功，远端main已核对。
- Token：精确 Token 用量不可读取；input/output/tool均无本次任务的精确计数。

视频 SHA256：aec73e975b1fd0a9f87f03dccff5f2ec6ff695794150cff98bf70d0011c165fb
