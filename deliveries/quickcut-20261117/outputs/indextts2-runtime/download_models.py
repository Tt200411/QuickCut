from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from huggingface_hub import hf_hub_download
import json, shutil
root = Path(__file__).resolve().parent / 'checkpoints'
jobs = []
for f in ['config.yaml','gpt.pth','s2mel.pth','bpe.model','wav2vec2bert_stats.pt','feat1.pt','feat2.pt']:
 jobs.append(('IndexTeam/IndexTTS-2','740dcaff396282ffb241903d150ac011cd4b1ede',f,root,f))
for f in ['config.json','preprocessor_config.json','model.safetensors']:
 jobs.append(('facebook/w2v-bert-2.0','da985ba0987f70aaeb84a80f2851cfac8c697a7b',f,root/'hf_cache/w2v-bert-2.0',f))
jobs += [('amphion/MaskGCT','265c6cef07625665d0c28d2faafb1415562379dc','semantic_codec/model.safetensors',root/'hf_cache','semantic_codec_model.safetensors'),('funasr/campplus','e4b6ede7ce16997aff4ae69fbca1f0175e2afede','campplus_cn_common.bin',root/'hf_cache','campplus_cn_common.bin')]
for f in ['config.json','bigvgan_generator.pt']:
 jobs.append(('nvidia/bigvgan_v2_22khz_80band_256x','633ff708ed5b74903e86ff1298cf4a98e921c513',f,root/'hf_cache/bigvgan',f))
def download(job):
 repo, rev, name, dest, out = job
 print('Downloading',repo,name,flush=True)
 p = Path(hf_hub_download(repo, name, revision=rev, local_dir=dest))
 target = dest/out
 if p != target:
  shutil.copy2(p,target)
 print('Ready',target,target.stat().st_size,flush=True)
with ThreadPoolExecutor(max_workers=3) as pool:
 list(pool.map(download,jobs))
