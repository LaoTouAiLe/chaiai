"""挂起提问与待发队列的隔离端到端服务；仅供自动化测试，不连接真实模型。"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT / 'scripts')]
import time

import uvicorn
from conftest import RoutingFakeModel
from langchain_core.messages import HumanMessage
from serve_fake import FAKE_KEY_ENV, _write_config

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.config import load_config

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8131)
parser.add_argument('--data', default=None)
args = parser.parse_args()
data = Path(args.data).resolve() if args.data else Path(tempfile.mkdtemp(prefix='chaiai-question-queue-'))
data.mkdir(exist_ok=True)
os.environ['CHAIAI_HOME'] = str(data / 'home')
os.environ[FAKE_KEY_ENV] = 'fake-scene-key'
dist = ROOT / 'frontend/dist'
config = load_config(_write_config(data, dist=dist, streaming=False))
config.jobs.background = True
config.memory.long_term.enabled = False
config.memory.compression.enabled = False
config.memory.curated.enabled = False
group = [
    {'question': '部署到哪个环境？', 'options': ['测试环境', '生产环境']},
    {'question': '用哪个分支？'},
    {'question': '要先备份吗？', 'options': ['要', '不要'], 'allow_free_text': False},
]
class SceneModel(RoutingFakeModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        latest = next((m.text for m in reversed(messages) if isinstance(m, HumanMessage)), '')
        if latest == 'STOP_SCENE':
            time.sleep(4)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

model = SceneModel(messages=iter(['fake']), decision='tool', reply='用户场景验证完成。', tool_calls_by_input={
    name: [{'name': 'ask_user', 'args': {'questions': group}, 'id': name.lower()}]
    for name in ['ASK_GROUP', 'ASK_SKIP', 'ASK_RETRY']
})
runtime = open_runtime(config, base_dir=data, model=model, static_dir=dist, instance_lock=False)
uvicorn.run(create_app(runtime), host='127.0.0.1', port=args.port, log_level='warning')
