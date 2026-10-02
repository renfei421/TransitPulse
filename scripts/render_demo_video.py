"""Create a four-minute screenshot walkthrough with a selectable Chinese subtitle track.

Run: uv run --no-project --with imageio-ffmpeg==0.6.0 python scripts/render_demo_video.py
This is an illustrated walkthrough, not a continuous recording of a live cloud run.
"""
from pathlib import Path
import subprocess

import imageio_ffmpeg


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "artifacts"
    scenes = [("demo-01-overview.png", 35), ("demo-02-fuel.png", 40),
              ("demo-03-public-transport.png", 35), ("demo-04-coverage.png", 35),
              ("demo-05-findings.png", 35), ("demo-06-engineering.png", 60)]
    if not all((folder / name).is_file() for name, _ in scenes):
        raise ValueError("Capture the actual browser views before rendering")
    # Encode fixed-frame-rate scenes separately; still-image concat timestamps
    # are not reliable enough to guarantee a four-minute walkthrough.
    lines = ["ffconcat version 1.0"]
    for i, (name, seconds) in enumerate(scenes, 1):
        clip = f"demo-scene-{i}.mp4"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", "10", "-i", name,
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-t", str(seconds), clip], cwd=folder, check=True)
        lines.append(f"file '{clip}'")
    (folder / "demo-storyboard.txt").write_text("\n".join(lines), encoding="utf-8")
    captions = [
        (0, 15, "TransitPulse：界面截图讲解版（无配音）。\n这是冻结结果回放，不是连续云端运行录像。"),
        (15, 35, "91 天事件窗口：124,491 条去重帖子，90,576 条完成推理。\n费用约 US$11.73 为模型记账，不包含云基础设施。"),
        (35, 55, "讨论量与有效态度分开统计。\n只有通过门控的作者目标态度才计入日均值。"),
        (55, 75, "每天至少 5 条有效态度；受限检索日排除出主要分析。\n缺失留空，不补零、不当作中性。"),
        (75, 95, "对公交的态度与对燃油成本的态度可以不同。\n分值为 P(正面) − P(负面)，confidence 单独保存。"),
        (95, 110, "事实、新闻转述和未提及对象不自动得到情感标签。\n模型质量仍需要人工金标评测。"),
        (110, 145, "Mastodon 作为补充平台，日有效态度不足。\n图表保留空白，不为了展示完整而制造趋势。"),
        (145, 165, "52 个有效日期对中，同期相关系数为 −0.0046。\nBootstrap 区间跨零，没有明显同期线性关联。"),
        (165, 180, "检索样本不代表总体民意。\n同期变化不是冲突或油价造成态度变化的因果证据。"),
        (180, 210, "调用前预留费用；保存原生响应后再写处理结果。\n可以重放缓存，每条文档跨重启最多三次付费预留。"),
        (210, 240, "18 个索引、434,092 条完整文档恢复指纹一致。\n11 组云端/本地 API 聚合一致；本地展示无需新增模型调用。"),
    ]
    def stamp(seconds):
        return f"{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02},000"
    subtitle = folder / "transitpulse-walkthrough.zh-CN.srt"
    subtitle.write_text("\n\n".join(f"{i}\n{stamp(start)} --> {stamp(end)}\n{text}"
                        for i, (start, end, text) in enumerate(captions, 1)) + "\n", encoding="utf-8")
    output = folder / "transitpulse-walkthrough.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", "demo-storyboard.txt", "-i", subtitle.name,
        "-c:v", "copy",
        "-c:s", "mov_text", "-metadata:s:s:0", "language=zho", "-disposition:s:0", "default",
        "-t", "240", "-movflags", "+faststart", str(output)], cwd=folder, check=True)
    print(f"Screenshot walkthrough: {output}; bytes={output.stat().st_size}; duration=240s; no narration")


if __name__ == "__main__":
    main()
