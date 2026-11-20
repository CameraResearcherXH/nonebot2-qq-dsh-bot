import os
import traceback
import logging

logger = logging.getLogger(__name__)

from time import time as ttime

import faiss
import librosa
import numpy as np
import parselmouth
import torch
import torch.nn.functional as F
from scipy import signal

from infer.hubert import extract_hubert_features
from tools.cuda_graph import cuda_graph_enabled, run_cuda_graph

bh, ah = signal.butter(N=5, Wn=48, btype="high", fs=16000)

# ===================== 低音区单独抬升（女声歌专用）=====================
# 背景（用户 2026-11-12）：
#   女声歌的低音段（句尾、念读段，实测 200~300Hz）变声后听着像男声；
#   但整首 +2 半音会把高音顶哑、共振峰整体上移发尖 —— 之前试过，不行。
#   所以要**只抬 F0 低于阈值的帧**，而不是整首移调。
#
# 实现方式：RVC 的 pitchf 是逐帧 F0，输出音高直接由它决定。
#   所以在 get_f0 里对「低音帧」乘一个额外的 2^(semis/12)，高音帧乘 1.0。
#   阈值附近用 FADE 宽度线性过渡，避免音高曲线在阈值处硬跳 2 个半音（会听出「折角」）。
#
# ⚠️ 副作用（必须知道）：低音区被抬升后，**低音到中音的音程被压扁**。
#    原曲 200→350Hz（9.7 半音），抬 2 个半音后变成 224→350Hz（7.8 半音）。
#    旋律线的低音部分会被「拉平」一点，所以幅度别贪，2 个半音起步。
#
# 全部走环境变量，0 = 关闭（默认关，别影响正常翻唱）：
#   RVC_LOW_LIFT_HZ     阈值（Hz），F0 低于它才开始抬；0 = 关闭
#   RVC_LOW_LIFT_SEMIS  最多抬几个半音
#   RVC_LOW_LIFT_FADE   过渡带宽（Hz），阈值上下各一半
LOW_LIFT_HZ = float(os.getenv("RVC_LOW_LIFT_HZ", "0") or 0)
LOW_LIFT_SEMIS = float(os.getenv("RVC_LOW_LIFT_SEMIS", "0") or 0)
LOW_LIFT_FADE = float(os.getenv("RVC_LOW_LIFT_FADE", "40") or 40)


def low_lift_gain(f0):
    """
    逐帧增益：F0 越低越接近 2^(semis/12)，越高越接近 1.0。

    例：阈值 260、过渡 40、+2 半音
        f0 <= 220  → ×1.1223（抬满 2 个半音）
        f0 = 260   → ×1.0595（抬 1 个半音）
        f0 >= 300  → ×1.0（不动）
    """
    if LOW_LIFT_HZ <= 0 or LOW_LIFT_SEMIS == 0:
        return 1.0
    fade = max(1.0, LOW_LIFT_FADE)
    w = (LOW_LIFT_HZ + fade - np.asarray(f0, dtype=np.float64)) / (2.0 * fade)
    w = np.clip(w, 0.0, 1.0)
    return np.power(2.0, LOW_LIFT_SEMIS * w / 12.0)


def change_rms(data1, sr1, data2, sr2, rate):  # 1是输入音频，2是输出音频,rate是2的占比
    # print(data1.max(),data2.max())
    rms1 = librosa.feature.rms(
        y=data1, frame_length=sr1 // 2 * 2, hop_length=sr1 // 2
    )  # 每半秒一个点
    rms2 = librosa.feature.rms(y=data2, frame_length=sr2 // 2 * 2, hop_length=sr2 // 2)
    rms1 = torch.from_numpy(rms1)
    rms1 = F.interpolate(
        rms1.unsqueeze(0), size=data2.shape[0], mode="linear"
    ).squeeze()
    rms2 = torch.from_numpy(rms2)
    rms2 = F.interpolate(
        rms2.unsqueeze(0), size=data2.shape[0], mode="linear"
    ).squeeze()
    rms2 = torch.max(rms2, torch.zeros_like(rms2) + 1e-6)
    data2 *= (
        torch.pow(rms1, torch.tensor(1 - rate))
        * torch.pow(rms2, torch.tensor(rate - 1))
    ).numpy()
    return data2


class Pipeline(object):
    def __init__(self, tgt_sr, config):
        self.x_pad, self.x_query, self.x_center, self.x_max, self.is_half = (
            config.x_pad,
            config.x_query,
            config.x_center,
            config.x_max,
            config.is_half,
        )
        self.sr = 16000  # hubert输入采样率
        self.window = 160  # 每帧点数
        self.t_pad = self.sr * self.x_pad  # 每条前后pad时间
        self.t_pad_tgt = tgt_sr * self.x_pad
        self.t_pad2 = self.t_pad * 2
        self.t_query = self.sr * self.x_query  # 查询切点前后查询时间
        self.t_center = self.sr * self.x_center  # 查询切点位置
        self.t_max = self.sr * self.x_max  # 免查询时长阈值
        self.device = config.device

    def get_f0(
        self,
        x,
        p_len,
        f0_up_key,
        f0_method,
    ):
        if f0_method not in ("pm", "rmvpe", "fcpe", "harvest", "crepe"):
            raise ValueError(f"Unsupported F0 method: {f0_method}")
        time_step = self.window / self.sr * 1000
        f0_min = 50
        f0_max = 1100
        f0_mel_min = 1127 * np.log(1 + f0_min / 700)
        f0_mel_max = 1127 * np.log(1 + f0_max / 700)
        if f0_method == "pm":
            f0 = (
                parselmouth.Sound(x, self.sr)
                .to_pitch_ac(
                    time_step=time_step / 1000,
                    voicing_threshold=0.6,
                    pitch_floor=f0_min,
                    pitch_ceiling=f0_max,
                )
                .selected_array["frequency"]
            )
            pad_size = (p_len - len(f0) + 1) // 2
            if pad_size > 0 or p_len - len(f0) - pad_size > 0:
                f0 = np.pad(
                    f0, [[pad_size, p_len - len(f0) - pad_size]], mode="constant"
                )
        elif f0_method == "rmvpe":
            if not hasattr(self, "model_rmvpe"):
                from infer.rmvpe import RMVPE

                logger.info(
                    "Loading rmvpe model,%s" % "%s/rmvpe.pt" % os.environ["rmvpe_root"]
                )
                self.model_rmvpe = RMVPE(
                    "%s/rmvpe.pt" % os.environ["rmvpe_root"],
                    is_half=self.is_half,
                    device=self.device,
                )
            f0 = self.model_rmvpe.infer_from_audio(x, thred=0.03)

            if "privateuseone" in str(self.device):  # clean ortruntime memory
                del self.model_rmvpe.model
                del self.model_rmvpe
                logger.info("Cleaning ortruntime memory")
        elif f0_method == "fcpe":
            if not hasattr(self, "model_fcpe"):
                from infer.fcpe import FCPEInfer

                logger.info("Loading fcpe model")
                self.model_fcpe = FCPEInfer(self.device)
            f0 = self.model_fcpe.infer(
                torch.from_numpy(x).unsqueeze(0).float(),
                sr=self.sr,
                decoder_mode="local_argmax",
                threshold=0.006,
            ).squeeze().detach().cpu().numpy()
        elif f0_method == "harvest":
            # parselmouth 自带 harvest，无需额外依赖
            f0 = (
                parselmouth.Sound(x, self.sr)
                .to_pitch_harvest(
                    time_step=time_step / 1000,
                    pitch_floor=f0_min,
                    pitch_ceiling=f0_max,
                )
                .selected_array["frequency"]
            )
            pad_size = (p_len - len(f0) + 1) // 2
            if pad_size > 0 or p_len - len(f0) - pad_size > 0:
                f0 = np.pad(
                    f0, [[pad_size, p_len - len(f0) - pad_size]], mode="constant"
                )
        elif f0_method == "crepe":
            # 神经网络 F0 提取，对弱音/气声/低信噪比更稳（需要 torchcrepe）
            # 注意：x 已经是 self.sr=16000，window=160 → hop 10ms，和 rmvpe 帧率一致
            import inspect

            import torchcrepe

            _dev = "cuda" if "cuda" in str(self.device) else "cpu"
            _x = np.asarray(x, dtype=np.float32)
            if _x.ndim > 1:
                _x = _x.mean(axis=-1)
            _in = torch.from_numpy(_x).unsqueeze(0)
            _kw = {
                "hop_length": self.window,
                "fmin": f0_min,
                "fmax": f0_max,
                "model": "full",
                "batch_size": 512,
                "device": _dev,
                "pad": True,
                "verbose": False,
                "return_periodicity": True,
            }
            # ⚠️ torchcrepe 各版本的 predict 关键字不一样 ——
            #    0.0.24 实测没有 verbose，硬传就是
            #    TypeError: predict() got an unexpected keyword argument 'verbose'。
            #    所以按真实签名过滤，缺哪个少传哪个，不猜版本。
            try:
                _params = inspect.signature(torchcrepe.predict).parameters
                if not any(p.kind == p.VAR_KEYWORD for p in _params.values()):
                    _drop = sorted(k for k in _kw if k not in _params)
                    _kw = {k: v for k, v in _kw.items() if k in _params}
                    if _drop:
                        print(f"ℹ️ [crepe] torchcrepe.predict 不接受 {_drop}，已跳过",
                              flush=True)
                    if "return_periodicity" not in _kw:
                        print("⚠️ [crepe] 这个版本没有 return_periodicity，"
                              "拿不到置信度 → 不做过低置信度置零，弱音段可能偏脏",
                              flush=True)
            except Exception as _e:
                print(f"ℹ️ [crepe] 读签名失败（{type(_e).__name__}），按默认参数调用",
                      flush=True)
            _out = torchcrepe.predict(_in, self.sr, **_kw)
            if isinstance(_out, (tuple, list)):
                _p, _per = _out[0], _out[1]
            else:
                _p, _per = _out, None
            if hasattr(_p, "detach"):
                _p = _p.detach().cpu().numpy()
            if hasattr(_per, "detach"):
                _per = _per.detach().cpu().numpy()
            f0 = np.asarray(_p, dtype=np.float64).reshape(-1)
            if _per is not None:
                _per = np.asarray(_per, dtype=np.float64).reshape(-1)
                _n = min(len(f0), len(_per))
                f0 = f0[:_n]
                # 置信度低（气声/清音）的帧置 0，交给下面的 uv 插值处理，
                # 和 rmvpe 的行为保持一致；阈值 0.21 是 torchcrepe 官方推荐
                f0[_per[:_n] < 0.21] = 0.0

        try:
            uv = f0 == 0
            f0[uv] = np.interp(np.where(uv)[0], np.where(~uv)[0], f0[~uv])
        except Exception:
            traceback.print_exc()
        # 低音区额外抬升：判定用**移调前**的原始 F0（Hz），符合直觉
        _lift = low_lift_gain(f0)
        if np.ndim(_lift) != 0:
            print(f"🎛️ [LOW_LIFT] 低音区 {LOW_LIFT_HZ:.0f}Hz 以下最多抬 "
                  f"{LOW_LIFT_SEMIS:+.1f} 半音（过渡 ±{LOW_LIFT_FADE:.0f}Hz）", flush=True)
            f0 = f0 * _lift
        f0 *= pow(2, f0_up_key / 12)
        f0bak = f0.copy()
        f0_mel = 1127 * np.log(1 + f0 / 700)
        f0_mel[f0_mel > 0] = (f0_mel[f0_mel > 0] - f0_mel_min) * 254 / (
            f0_mel_max - f0_mel_min
        ) + 1
        f0_mel[f0_mel <= 1] = 1
        f0_mel[f0_mel > 255] = 255
        f0_coarse = np.rint(f0_mel).astype(np.int32)
        return f0_coarse, f0bak  # 1-0

    def vc(
        self,
        model,
        net_g,
        sid,
        audio0,
        pitch,
        pitchf,
        times,
        index,
        index_vectors,
        index_rate,
        version,
        protect,
    ):
        feats = torch.from_numpy(audio0)
        if self.is_half:
            feats = feats.half()
        else:
            feats = feats.float()
        if feats.dim() == 2:  # double channels
            feats = feats.mean(-1)
        assert feats.dim() == 1, feats.dim()
        feats = feats.view(1, -1)
        padding_mask = torch.BoolTensor(feats.shape).to(self.device).fill_(False)

        t0 = ttime()
        with torch.no_grad():
            feats = extract_hubert_features(
                model,
                feats.to(self.device),
                version,
                padding_mask=padding_mask,
            )
        if protect < 0.5 and pitch is not None and pitchf is not None:
            feats0 = feats.clone()
        if (
            not isinstance(index, type(None))
            and not isinstance(index_vectors, type(None))
            and index_rate != 0
        ):
            npy = feats[0].cpu().numpy()
            if self.is_half:
                npy = npy.astype("float32")

            score, ix = index.search(npy, k=8)
            weight = np.square(1 / score)
            weight /= weight.sum(axis=1, keepdims=True)
            npy = np.sum(index_vectors[ix] * np.expand_dims(weight, axis=2), axis=1)

            if self.is_half:
                npy = npy.astype("float16")
            feats = (
                torch.from_numpy(npy).unsqueeze(0).to(self.device) * index_rate
                + (1 - index_rate) * feats
            )

        feats = F.interpolate(feats.permute(0, 2, 1), scale_factor=2).permute(0, 2, 1)
        if protect < 0.5 and pitch is not None and pitchf is not None:
            feats0 = F.interpolate(feats0.permute(0, 2, 1), scale_factor=2).permute(
                0, 2, 1
            )
        t1 = ttime()
        p_len = audio0.shape[0] // self.window
        if feats.shape[1] < p_len:
            p_len = feats.shape[1]
            if pitch is not None and pitchf is not None:
                pitch = pitch[:, :p_len]
                pitchf = pitchf[:, :p_len]

        if protect < 0.5 and pitch is not None and pitchf is not None:
            pitchff = pitchf.clone()
            pitchff[pitchf > 0] = 1
            pitchff[pitchf < 1] = protect
            pitchff = pitchff.unsqueeze(-1)
            feats = feats * pitchff + feats0 * (1 - pitchff)
            feats = feats.to(feats0.dtype)
        p_len = torch.tensor([p_len], device=self.device).long()
        with torch.no_grad():
            hasp = pitch is not None and pitchf is not None
            if hasp:
                synthesized = run_cuda_graph(
                    net_g,
                    "rvc-synth-f0",
                    lambda phone, lengths, coarse, continuous, speaker: net_g.infer(
                        phone, lengths, coarse, continuous, speaker
                    )[0],
                    feats,
                    p_len,
                    pitch,
                    pitchf,
                    sid,
                )
            else:
                synthesized = run_cuda_graph(
                    net_g,
                    "rvc-synth-no-f0",
                    lambda phone, lengths, speaker: net_g.infer(
                        phone, lengths, speaker
                    )[0],
                    feats,
                    p_len,
                    sid,
                )
            audio1 = synthesized[0, 0].data.cpu().float().numpy()
            del hasp, synthesized
        del feats, p_len, padding_mask
        if torch.cuda.is_available() and not cuda_graph_enabled(self.device):
            torch.cuda.empty_cache()
        t2 = ttime()
        times[0] += t1 - t0
        times[2] += t2 - t1
        return audio1

    def pipeline(
        self,
        model,
        net_g,
        sid,
        audio,
        times,
        f0_up_key,
        f0_method,
        file_index,
        index_rate,
        if_f0,
        tgt_sr,
        resample_sr,
        rms_mix_rate,
        version,
        protect,
    ):
        if (
            file_index != ""
            and os.path.exists(file_index)
            and index_rate != 0
        ):
            try:
                index = faiss.read_index(file_index)
                index_vectors = index.reconstruct_n(0, index.ntotal)
            except:
                traceback.print_exc()
                index = index_vectors = None
        else:
            index = index_vectors = None
        audio = signal.filtfilt(bh, ah, audio)
        audio_pad = np.pad(audio, (self.window // 2, self.window // 2), mode="reflect")
        opt_ts = []
        if audio_pad.shape[0] > self.t_max:
            audio_sum = np.zeros_like(audio)
            for i in range(self.window):
                audio_sum += np.abs(audio_pad[i : i - self.window])
            for t in range(self.t_center, audio.shape[0], self.t_center):
                opt_ts.append(
                    t
                    - self.t_query
                    + np.where(
                        audio_sum[t - self.t_query : t + self.t_query]
                        == audio_sum[t - self.t_query : t + self.t_query].min()
                    )[0][0]
                )
        s = 0
        audio_opt = []
        t = None
        t1 = ttime()
        audio_pad = np.pad(audio, (self.t_pad, self.t_pad), mode="reflect")
        p_len = audio_pad.shape[0] // self.window
        sid = torch.tensor(sid, device=self.device).unsqueeze(0).long()
        pitch, pitchf = None, None
        if if_f0 == 1:
            pitch, pitchf = self.get_f0(
                audio_pad,
                p_len,
                f0_up_key,
                f0_method,
            )
            pitch = pitch[:p_len]
            pitchf = pitchf[:p_len]
            pitchf = pitchf.astype(np.float32)
            pitch = torch.tensor(pitch, device=self.device).unsqueeze(0).long()
            pitchf = torch.tensor(pitchf, device=self.device).unsqueeze(0).float()
        t2 = ttime()
        times[1] += t2 - t1
        for t in opt_ts:
            t = t // self.window * self.window
            if if_f0 == 1:
                audio_opt.append(
                    self.vc(
                        model,
                        net_g,
                        sid,
                        audio_pad[s : t + self.t_pad2 + self.window],
                        pitch[:, s // self.window : (t + self.t_pad2) // self.window],
                        pitchf[:, s // self.window : (t + self.t_pad2) // self.window],
                        times,
                        index,
                        index_vectors,
                        index_rate,
                        version,
                        protect,
                    )[self.t_pad_tgt : -self.t_pad_tgt]
                )
            else:
                audio_opt.append(
                    self.vc(
                        model,
                        net_g,
                        sid,
                        audio_pad[s : t + self.t_pad2 + self.window],
                        None,
                        None,
                        times,
                        index,
                        index_vectors,
                        index_rate,
                        version,
                        protect,
                    )[self.t_pad_tgt : -self.t_pad_tgt]
                )
            s = t
        if if_f0 == 1:
            audio_opt.append(
                self.vc(
                    model,
                    net_g,
                    sid,
                    audio_pad[t:],
                    pitch[:, t // self.window :] if t is not None else pitch,
                    pitchf[:, t // self.window :] if t is not None else pitchf,
                    times,
                    index,
                    index_vectors,
                    index_rate,
                    version,
                    protect,
                )[self.t_pad_tgt : -self.t_pad_tgt]
            )
        else:
            audio_opt.append(
                self.vc(
                    model,
                    net_g,
                    sid,
                    audio_pad[t:],
                    None,
                    None,
                    times,
                    index,
                    index_vectors,
                    index_rate,
                    version,
                    protect,
                )[self.t_pad_tgt : -self.t_pad_tgt]
            )
        audio_opt = np.concatenate(audio_opt)
        if rms_mix_rate != 1:
            audio_opt = change_rms(audio, 16000, audio_opt, tgt_sr, rms_mix_rate)
        if tgt_sr != resample_sr >= 16000:
            audio_opt = librosa.resample(
                audio_opt, orig_sr=tgt_sr, target_sr=resample_sr
            )
        audio_max = np.abs(audio_opt).max() / 0.99
        max_int16 = 32768
        if audio_max > 1:
            max_int16 /= audio_max
        audio_opt = (audio_opt * max_int16).astype(np.int16)
        del pitch, pitchf, sid
        if torch.cuda.is_available() and not cuda_graph_enabled(self.device):
            torch.cuda.empty_cache()
        return audio_opt

