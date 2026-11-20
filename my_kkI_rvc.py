import sys
import os
import argparse
import traceback

# 强制把标准输出设为 UTF-8，防止任何编码报错
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except:
        pass

# 设置基础路径
rvc_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, rvc_root)

# 强行设置 RVC 需要的系统环境变量
os.environ["rmvpe_root"] = os.path.join(rvc_root, "assets", "rmvpe")
os.environ["hubert_root"] = os.path.join(rvc_root, "assets", "hubert")
os.environ["pretrained_root"] = os.path.join(rvc_root, "assets", "pretrained_v2")
os.environ["weight_root"] = os.path.join(rvc_root, "assets", "weights")
os.environ["index_root"] = os.path.join(rvc_root, "assets", "weights")

# 导入依赖
import soundfile as sf
from configs.config import Config
try:
    from infer.modules.vc.modules import VC
except ImportError:
    try:
        from infer.vc.modules import VC
    except ImportError:
        from infer.module.modules import VC

# 杀掉自动寻找 index 的函数，防止底层报错
import infer.vc.utils as vc_utils
import infer.vc.modules as vc_modules
vc_utils.get_index_path_from_model = lambda sid: ""
vc_modules.get_index_path_from_model = lambda sid: ""

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--pitch", type=int, default=0)
    # 新增：与算力机当前写死的默认值保持一致，不传时行为完全不变
    parser.add_argument("--f0_method", default="rmvpe")
    parser.add_argument("--index_rate", type=float, default=0.5)
    parser.add_argument("--rms_mix_rate", type=float, default=0.25)
    parser.add_argument("--protect", type=float, default=0.25)
    args = parser.parse_args()

    # 👇 全英文打印，杜绝任何编码报错
    print("[RVC] Init Config and load model...", flush=True)
    
    sys.argv = [sys.argv[0]]
    
    try:
        config = Config()
        config.device = "cuda:0"
        config.is_half = True
        
        vc = VC(config)
        
        print(f"[RVC] Loading model: {args.model}", flush=True)
        vc.get_vc(args.model)
        
        print("[RVC] Model loaded! Start inference...", flush=True)
        
        result = vc.vc_single(
            sid=0,
            input_audio_path=args.input,
            f0_up_key=args.pitch,
            f0_method=args.f0_method,
            file_index=args.index,
            index_rate=args.index_rate,
            resample_sr=0,
            rms_mix_rate=args.rms_mix_rate,
            protect=args.protect,
        )
        
        print("[RVC] Inference done, writing file...", flush=True)
        
        opt = result[1]
        if isinstance(opt, tuple) and len(opt) == 2:
            sr, audio_data = opt
            sf.write(args.output, audio_data, sr)
            print(f"[RVC] Success saved to: {args.output}", flush=True)
            sys.exit(0)
        else:
            print("[RVC] Failed: Invalid audio data format.", flush=True)
            sys.exit(1)

    except Exception as e:
        print(f"[RVC] CRASH: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
