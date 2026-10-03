import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest import TestCase, skipUnless

from ..codex_service import CodexService


class QuotaResponseTests(TestCase):
    def test_official_multi_bucket_view_is_kept(self):
        service = object.__new__(CodexService)
        buckets = {"codex": {"primary": {"windowDurationMins": 10080}}}
        response = service._store_quota_result(
            {"rateLimitsByLimitId": buckets, "secret": "must-not-forward"},
            source="app_server_rpc",
        )
        self.assertEqual(response["rateLimitsByLimitId"], buckets)
        self.assertNotIn("secret", response)


@skipUnless(shutil.which("node"), "Node is needed to execute the page's JavaScript")
class QuotaDisplayTests(TestCase):
    def test_rendered_cards_match_duration_for_pro_and_plus(self):
        html = (Path(__file__).parents[1] / "pages/account/index.html").read_text()
        names = (
            "percent", "durationText", "countdownText", "quotaBucket", "quotaWindow",
            "quotaUsedPercent", "quotaResetAt", "quotaDurationText",
            "renderQuotaCard", "renderQuota",
        )
        functions = "\n".join(
            re.search(r"^  function " + name + r"\(.*$", html, re.M).group()
            for name in names
        )
        script = '''
const assert=require('node:assert/strict');
const elements=new Map();
function $(id){if(!elements.has(id))elements.set(id,{style:{},classList:{toggle(){}}});return elements.get(id);}
''' + functions + '''
const week={usedPercent:42,windowDurationMins:10080,resetsAt:2000000000};
const short={usedPercent:17,windowDurationMins:300,resetsAt:1900000000};
for(const planType of ['pro100','pro200']){
  renderQuota({rateLimits:{planType,primary:week,secondary:null}});
  assert.equal($('quota5hRate').textContent,'—');
  assert.equal($('quota7dRate').textContent,'42%');
  assert.equal($('quota7dWindow').textContent,'7 天');
  renderQuota({rateLimits:{planType,primary:week,secondary:short}});
  assert.equal($('quota5hRate').textContent,'17%');
  assert.equal($('quota7dRate').textContent,'42%');
}
renderQuota({rateLimits:{primary:short,secondary:week}});
assert.equal($('quota5hRate').textContent,'17%');
assert.equal($('quota7dRate').textContent,'42%');
renderQuota({rateLimits:{primary:short},rateLimitsByLimitId:{codex:{primary:week},other:{primary:short}}});
assert.equal($('quota5hRate').textContent,'—');
assert.equal($('quota7dRate').textContent,'42%');
renderQuota({rateLimits:{primary:{usedPercent:9},secondary:{usedPercent:11,windowDurationMins:15}}});
assert.equal($('quota5hRate').textContent,'—');
assert.equal($('quota7dRate').textContent,'—');
renderQuota({rateLimits:{primary:{usedPercent:null,windowDurationMins:300}}});
assert.equal($('quota5hRate').textContent,'—');
renderQuota({rateLimits:{windows:[{used_percent:0,window_duration_mins:300},week]}});
assert.equal($('quota5hRate').textContent,'0%');
assert.equal($('quota7dRate').textContent,'42%');
console.log(JSON.stringify({cases:10,result:'passed'}));
'''
        result = subprocess.run([shutil.which("node"), "-e", script], text=True,
                                capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout)["result"], "passed")
