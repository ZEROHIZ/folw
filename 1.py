import json
import requests

# 1. 配置参数（根据你的实际情况修改）
BASE_URL = "http://192.168.110.30:5566"  # 你的 Docker 端口映射是 3800
API_KEY = "sk-VdJ4DV8srDJVKYzbC1eWuokjohrWRfAqu5IQG29jptOoANUj"  # 对应 setting.toml 里的 [global].api_key（默认是 han1234）

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def test_image_generation():
    print("\n========== [1. 测试图片生成] ==========")
    url = f"{BASE_URL}/v1/chat/completions"

    # 图片模型可选：
    # - gemini-3.0-pro-image-landscape (16:9)
    # - gemini-3.0-pro-image-portrait (9:16)
    # - gemini-3.0-pro-image-square (1:1)
    # - gemini-3.1-flash-image-landscape
    payload = {
        "model": "Nano-Banana-2.1",
        "messages": [
            {
                "role": "user",
                "content": "一只可爱的小猫趴在窗台上晒太阳，阳光明媚，超高清",
            }
        ],
        "stream": False,
    }

    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=120)
        print(f"Status Code: {response.status_code}")
        result = response.json()
        print("响应结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # 提取图片 URL
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"\n👉 生成的图片内容/链接: {content}")
    except Exception as e:
        print(f"❌ 请求失败: {e}")


def test_video_generation():
    print("\n========== [2. 测试视频生成 (Veo)] ==========")
    url = f"{BASE_URL}/v1/chat/completions"

    # 视频模型可选：
    # - veo_3_1_t2v_fast_landscape (文生视频-横屏 16:9)
    # - veo_3_1_t2v_fast_portrait  (文生视频-竖屏 9:16)
    # - veo_3_1_t2v_fast_ultra     (超高清文生视频)
    payload = {
        "model": "veo_3_1_t2v_fast_landscape",
        "messages": [
            {
                "role": "user",
                "content": "A golden retriever running happily on the green grass, cinematic lighting, 4k",
            }
        ],
        "stream": False,
    }

    try:
        print("正在提交视频生成任务（耗时较长，请耐心等待 1~3 分钟）...")
        response = requests.post(url, headers=HEADERS, json=payload, timeout=600)
        print(f"Status Code: {response.status_code}")
        result = response.json()
        print("响应结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        # 提取视频 URL
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"\n👉 生成的视频链接: {content}")
    except Exception as e:
        print(f"❌ 请求失败: {e}")


if __name__ == "__main__":
    # 先测图片
    test_image_generation()

    # 如需测试视频，取消下行注释
    # test_video_generation()