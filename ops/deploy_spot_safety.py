#!/usr/bin/env python3
"""Pure, read-only safety classification for immutable deploy cutovers.

This module never calls Binance and never writes state.  The deploy script owns
the GET-only observations and passes immutable values here for classification.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


SAFE_MANAGED_SPOT = "SAFE_MANAGED_SPOT"
SAFE_NO_SPOT = "SAFE_NO_SPOT"
ACTIVE_SPOT_ORDER_STATUSES = frozenset({"NEW", "PENDING_NEW"})
SPOT_STOP_TYPES = frozenset({"STOP_LOSS_LIMIT"})
SPOT_TAKE_PROFIT_TYPES = frozenset({"LIMIT_MAKER"})

# Existing repository contracts, not deploy-specific thresholds:
# - audit_pipeline.evaluate_spot_position_reconciliation uses max(step, 1e-12)
#   for managed-vs-observed Spot quantity alignment.
# - pre_entry_safety_gate.DEFAULT_PROTECTION_TOLERANCE is 1e-6.
MIN_QUANTITY_TOLERANCE = Decimal("1e-12")
DEFAULT_PROTECTION_TOLERANCE = Decimal("1e-6")

SPOT_CRITICAL_FILES = (
    "trading/atomic_persistence.py",
    "trading/binance_client.py",
    "trading/bot.py",
    "trading/config.py",
    "trading/config_loader.py",
    "trading/history.py",
    "trading/longs.py",
    "trading/market.py",
    "trading/orchestration/audit_pipeline.py",
    "trading/orchestration/position_lifecycle.py",
    "trading/residuals.py",
    "trading/sl_guardian.py",
)

PREVENTIVE_SPOT_CLOSE_PATH = "trading/preventive_spot_close.py"

# Named AST-node deltas only; unchanged nodes are compared between trees.
# Each safety contract additionally requires its own structural predicates.
_AUDITED_NODE_DELTAS = json.loads(r'''{
  "trading/auto_loop.py": {
    "Expr:sys.path.insert(0, os.path.dirname(__file__))#1": [null,"93fddc22824bf669fbe3af53db2af4790b9a8f970caf06b8fde1306e391e08cb"],
    "FunctionDef:market_sell_all#1": ["8dc9843017d8f297d623bbe25f89ff7a36dda2ed3fb5a6c37c9a32cddf4e6fde","2b7bf184a34d8d19c70b3b8bf16722f6d5e675a7d103af86ff9b6a1b4433dee4"],
    "FunctionDef:place_market_buy#1": ["8a92bd735b4a263a9e0b377d445b1e8c75dc2fd981960cbabfcbab29935ed535","bad9c74949e60cd2b32aa384568eaa5926d093b5c2d0e083aa313b0758f0f58d"],
    "FunctionDef:place_oco#1": ["6af3696d4721fe1d0d6da4b45d9e552d34f07132093238b440af3c2f8117eef0","1f38b8bbde1d1d189336cf16f98b88ea76c5d16a0315f55db4cb5cb78b0cbb39"],
    "FunctionDef:take_partial_profit#1": ["73d25ea130d1e8ad86c5b6e08d3293a80e18a94fa186f6fcf791f175f758b822","970da39d891f9050c0b2743c5e4ab36e02897b19f43cfca1a6622aad1555480d"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity, normalize_quantity_to_step#1": [null,"13ad33d6d526b84f7f36870f2aa7c6acf44f017dc92d84e653d02bd9faaa521f"]
  },
  "trading/capability_history.py": {
    "Assign:CAPABILITIES#1": ["547e5d072e0a7d5b47762aa50526e1f3058cb09c1825f5fbbd067b44222b2cc4","9026c22bad8ab6d853eb0c8b6b951a4600e52a1bc688e914458513dcad8495ec"]
  },
  "trading/check_version_consistency.py": {
    "FunctionDef:validate#1": ["425d25828117571e04d8133caacf778cf865c2e763f8362a267e529972b25f25","ed48d9c293b6ed7ffa2a49139d5bb1b7f8c59f7d0aa286748f009592d26c73e8"]
  },
  "trading/entry_spot_recovery.py": {
    "Assign:KIND#1": [null,"1146b1c66ed2ebcd7e74276fa65fef28061beabd90969e96021e68fbb0abb7e3"],
    "Expr:'Evidence-gated recovery for an unprotected Spot LONG create#1": [null,"b7693ad7f113ff9abda2c6b2a5d12a2371fff30afe172f0e918f6e6c29ad21f3"],
    "FunctionDef:_client_order_id#1": [null,"9412d19141401a6380f484ee9f6b5dcb27dfb32dd66ebd54ffc0f5fbc18377fd"],
    "FunctionDef:_confirm_existing_oco#1": [null,"c53433713617eb0de758f7632b3f3570031051b1a57e6fd17d907deea56ceee2"],
    "FunctionDef:_restore_managed_oco#1": [null,"c5b16472d509f1333aa644a289068ef97344d58b299d851be920aa9bdd4ccc5a"],
    "FunctionDef:_result#1": [null,"d351df47aff498d9b3df3ad42241cf9a9024f8245325773dd0cc726b738a08bc"],
    "FunctionDef:_validate_order#1": [null,"cd9db80a7584924654a9dbe5d67fecefd4074b0bb8681d9f3109739a82307180"],
    "FunctionDef:prepare_entry_recovery#1": [null,"0f6fb8c16f85a1b78e181454402aa3175e07c5e040f18909dad683cf2aad23dc"],
    "FunctionDef:reconcile_pending_entry_spot_long#1": [null,"299472c0460743512d556a35eb94d6839bc4791445f5b0b7fde7fc6b3ca32681"],
    "FunctionDef:submit_entry_emergency_sell#1": [null,"1408b2a46b1a6c1972b4d314c1de4130bb502b9d970cbe6b3d8de60ae92864d0"],
    "Import:import hashlib#1": [null,"092cb77ae0770f1627b29bf41b2732722fbcc3a0e1d77a79674801800a2721d1"],
    "Import:import partial_spot_long as spot#1": [null,"98977c1a5620d7e509a9521b0eec1c95ee4f8369991dc3170a3611689e7520b3"],
    "Import:import residuals#1": [null,"adbbed05f36e8bf334c4550944951b9473ed464376152819853c88b21112da54"],
    "Import:import time#1": [null,"ff05bad0fd9a7212d3cd85fd2726055168be21bc8f6e0d93b29965794b0675d4"],
    "Import:import utils#1": [null,"3d2cfb26755096f58e24c12650e116cfd94c0309370f1de7035bfde604470759"],
    "ImportFrom:from decimal import Decimal#1": [null,"f4fac24d7774ccf6312fc8ec74083b5af2b6ff0a183153b3bf747f246c34d3e1"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity, remaining_after_execution#1": [null,"c80db0a9c49999f03f571d7215d42e98ca97fe4dc8dee34a553fb6093d9b5a38"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/longs.py": {
    "FunctionDef:_build_oco_params#1": ["8748659100f4e600ada227147b11d30774bda4f63eb57a5490dee9b23b137b62","d4f5d279b0da1f46ba9b415aa7f39609165e189e6bbceb7d507453381ad90c6b"],
    "FunctionDef:_market_sell#1": ["3a3d1c9ecdc0d6865b8fe09e8491c56bd09fea01464743a5b25f4f6b9783a731","beaee331a3618ff7230156615e34e8619f30da636c48eaf817d19e8999ccfe25"],
    "FunctionDef:_recolocar_oco#1": ["bd1943fddfd33f3b844b51912b6b20f0805a15767e2a43c2fec72d27684c3550","61937065cd6581e8635302ba57bb359cedae1989a2bc5221ad1b7ed2e7fe4a58"],
    "FunctionDef:manage_long#1": ["42e65d5d816d3f03796c5d5cdd21a308eaa4a1dae2017ab38142474bb5d28b6d","d06d60b68edcd0909d43045e7b21fc153eaaa0b428abea397091c1c2279f966a"],
    "FunctionDef:open_long#1": ["86b7690b98df174415577ea7045aff3c93521db40c0ac5fe047b25136403e159","1624f01bf27b4a7d0ecbf6aaa15cd2c17bbda01d96b79c902cd6e562b3d0a025"],
    "Import:import entry_spot_recovery#1": [null,"d3b7a1777f34f71ee46be540b7291f2d8a65817a889d3cd4823c9b51c5c8e268"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity, normalize_quantity_to_step#1": [null,"13ad33d6d526b84f7f36870f2aa7c6acf44f017dc92d84e653d02bd9faaa521f"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/orchestration/audit_pipeline.py": {
    "FunctionDef:audit_orphans#1": ["db06692a3c6dcbe9d7853b75cf978e488439a87205a6b300b2c1614348073fc7","21f76f6ba36c92bdc68ca67c69638851fbc656354c9b4290b78770c1541c8dab"],
    "FunctionDef:reconcile_stale_spot_positions#1": ["901182668a053325a644fac42634ac9cf356a8199fc375824f6ff4f455e44039","3c99f2e86e147807acaffad708c7c80fb78510229262a90400fcc214d0f265e7"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity#1": [null,"78796685355c129630527542047b70b12a3326531694998ce637553e2f7f2fe4"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/orchestration/cycle_runner.py": {
    "CycleRunner.FunctionDef:_handle_preventive_long_spot#1": [null,"7f5812b35138ee764bd2a5f44a110fac7b0af6cb588b7efc56abe2e92048b0a1"],
    "CycleRunner.FunctionDef:_reconcile_pending_spot_long#1": [null,"effce13443764fd2212fb05c8182ea50647336352278bf665e6b03ddb4f4c83d"],
    "CycleRunner.FunctionDef:run#1": ["70a64cfde1beaf363de7902d2ca47d71f31b21cf9dc8c6abd974427ad75d3eaa","48d50f2d8809f1ab92895451dc4424fd8db591fb95d44d23cc09d15ebb74db5b"],
    "Import:import entry_spot_recovery#1": [null,"d3b7a1777f34f71ee46be540b7291f2d8a65817a889d3cd4823c9b51c5c8e268"],
    "Import:import partial_spot_long#1": [null,"117f6f9766beee76d89655cb1750356aa8edfd44b690045dadebf4ccad6dbec1"],
    "Import:import preventive_spot_close#1": [null,"696cb385cf71b97b4e03187924ca1c7522cac28c22ead78fa110ccf9e7ac5033"],
    "ImportFrom:from orchestration import position_lifecycle#1": [null,"135b291821b0361641f92dc90cc0ad666a55c4b5abff67d6f24938773dd25bcd"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending, spot_long_recovery_kind#1": [null,"081b9a73e76f8fb8473530eb22404e80f4acdfed310ef4872e126fda7d224dc8"]
  },
  "trading/orchestration/position_lifecycle.py": {
    "FunctionDef:check_partial_long#1": ["fbc49462037b989076c39951e20a6b743ecc52847a9d700222e25f7c6b8e3e42","23225d2be60d2405cb4c73fa7ef8388387e4e4720f642e7efc2bce87b2933a75"],
    "FunctionDef:finalize_confirmed_partial_long#1": [null,"d62586dde775eebf663bcecef3ea7cd18358c6f78d51471f9d8534824e5b06f4"],
    "FunctionDef:recolocar_oco_long#1": ["c308be786483a5fb795f46cdcbd0809e9ce1d7187bd7727613c620047617a65e","cc1d04d4c563844a378bd355d167b54581bc1a0b736c320f80d90b84f02698ed"],
    "Import:import partial_spot_long#1": [null,"117f6f9766beee76d89655cb1750356aa8edfd44b690045dadebf4ccad6dbec1"],
    "ImportFrom:from quantity_integrity import compute_partial_and_remaining, decimal_value, format_decimal_quantity, normalize_quantity_to_step, remaining_after_execution#1": [null,"3d041ba814da8b08e342ada4ba4a10b41fa2928deafcc8f9f71201dabc1fe603"],
    "ImportFrom:from quantity_integrity import compute_partial_and_remaining, decimal_value, remaining_after_execution#1": ["a73eb943aa410829e128fae8e1757a56370f94087a917e35cd229620fb04b535",null],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/partial_spot_long.py": {
    "Expr:'Fail-closed exchange flow for a managed Spot LONG partial c#1": [null,"43fec0548745606bc132511f44f352029a2888860609e10e5d3e752af2e3e2f8"],
    "FunctionDef:_accept_oco#1": [null,"92d719ab78bbee362220b540d37f27b080aff2ef05e11d9dd30ce4e6ecc4d620"],
    "FunctionDef:_asset#1": [null,"10c58141ce516f984766d514c510c6e1a802a2372a4ffa679ce27871c870ed7b"],
    "FunctionDef:_balance#1": [null,"87f1b1e324ebab9fdfca4adcba5d49b007aece6bcee448470a680242cf4cfcde"],
    "FunctionDef:_\u0063ancel_order_list#1": [null,"939216c3ce0ea02a6e74da4b374eb2fa902e9240df056197603c12bf255e992b"],
    "FunctionDef:_client_order_id#1": [null,"f5b45146530efb686fc31b04040872864b3516c74bacf8bd59943d89131b92d1"],
    "FunctionDef:_create_oco#1": [null,"c035d224be36b60599fa9b0521b5f5e1308c901e4a880fb32a7da705a9111fe7"],
    "FunctionDef:_\u0063reate_order#1": [null,"b1115269f92570759db6a6334a7ef1522a163a918666a1f3836bc70274a8e59b"],
    "FunctionDef:_decimal#1": [null,"1f4f7997879acdf6ae906f9810b9dbd5ac84df67295b81a4f613a795f32b9953"],
    "FunctionDef:_fill_evidence#1": [null,"3aae35b703a2d0e9e09d7b838d6de00b93d5830ef93be0d45458f68a8093e83e"],
    "FunctionDef:_filters#1": [null,"d3c1ac0f1126411d80a83e360b4074002789d47d6df6b01a323023916aa31947"],
    "FunctionDef:_get_order#1": [null,"9a6aa7105151203b826d1a34583da241f200a161a688934163da064a6b253b09"],
    "FunctionDef:_get_order_list#1": [null,"30d5481e6397ac076a85f5fdc24a80b6175470e2202368944d1346f2fdfaa551"],
    "FunctionDef:_mark_unprotected#1": [null,"aeb80c662a8c2d6a1b834e5be15bc4cc75e96727d4ccee0bbb46fd523ff66cd6"],
    "FunctionDef:_normalize#1": [null,"88789574b9343a32e91a7cbd5517167262b435bc87a60c469884ed0d0ed5ec29"],
    "FunctionDef:_oco_payload#1": [null,"ada1804381bcbdd86ac228646e34eab79e0bf3f1e395d8610b223e8671d31847"],
    "FunctionDef:_open_orders#1": [null,"d1c8aab78bc7eba27e10252cfde5a5e16991f2a0b4d40f31fd72e5ed6f5dee11"],
    "FunctionDef:_restore_snapshot#1": [null,"e92cc6384f3a4ba1030637d6a7f2544c3e90abfdbe2bd7c57ddc585885a28d61"],
    "FunctionDef:_result#1": [null,"029870f2715ddd37fc346eb7da007a69fdf68358a0e50ff97ec6c1ad7e03e393"],
    "FunctionDef:_validate_oco_snapshot#1": [null,"b301b89f65ba71319d09e3be681ea29de4263102ea85e0fd34d3e2a27f333cbb"],
    "FunctionDef:attempt_partial_long_spot#1": [null,"5c7b6bccb119786dbff9b6c70888292950ec39f39ec1a845f65d02d8b79c73f2"],
    "FunctionDef:reconcile_pending_partial_long_spot#1": [null,"12f8a74ec5f4b6db732fb009196f79e4c1371ea6c29fc0fe8b50cdae863aa544"],
    "Import:import config#1": [null,"1dfaa2106dcbd53b68fc211b1fdf4aaad5286b2660ee0830cb825b00c00d67be"],
    "Import:import hashlib#1": [null,"092cb77ae0770f1627b29bf41b2732722fbcc3a0e1d77a79674801800a2721d1"],
    "Import:import residuals#1": [null,"adbbed05f36e8bf334c4550944951b9473ed464376152819853c88b21112da54"],
    "Import:import time#1": [null,"ff05bad0fd9a7212d3cd85fd2726055168be21bc8f6e0d93b29965794b0675d4"],
    "Import:import utils#1": [null,"3d2cfb26755096f58e24c12650e116cfd94c0309370f1de7035bfde604470759"],
    "ImportFrom:from decimal import Decimal, InvalidOperation#1": [null,"d1988081d4cbf2449dcd2afe33a0784e64bff05675e47c95439fa04d797ec0ed"],
    "ImportFrom:from quantity_integrity import compute_partial_and_remaining, decimal_value, format_decimal_quantity, normalize_quantity_to_step, remaining_after_execution#1": [null,"3d041ba814da8b08e342ada4ba4a10b41fa2928deafcc8f9f71201dabc1fe603"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"],
    "ImportFrom:from urllib.error import HTTPError#1": [null,"6253e89057ec0abb9ae4a242e12358e82236f1ac40eb05796c1dffaa8c5d7ce2"]
  },
  "trading/preventive_spot_close.py": {
    "Assign:FINAL_CLOSE_STATUSES#1": [null,"81eda500e82fb072ea57d79c6b216ea4a8ea4346d0059623cee2a65a6a2a4080"],
    "Expr:'Fail-closed preventive close lifecycle for managed LONG Spo#1": [null,"1a3a18e2ddbe0b854bba9197d181d763d50588148c798806a50603c80857e445"],
    "FunctionDef:_asset_from_symbol#1": [null,"d7e4cf6b9f26ad51518b5e23e5f97d48563776650000644968a1b293d8455bdb"],
    "FunctionDef:_balance#1": [null,"80b2a2e8f6b0f591604a5a90708f9c2eb051ca23e7e20578379479fadc19d55b"],
    "FunctionDef:_client_order_id#1": [null,"00d7ba6fc89d18cd939514398a20127a161d16dabd4bbf5bf5049b36daf8e0d2"],
    "FunctionDef:_decimal#1": [null,"71683f694692cea966494504441288a0cbcfe178f2ec0fb4360e05fa1d1c82bb"],
    "FunctionDef:_error_text#1": [null,"9e2e6917871cc3cac5ad4e12ac82aff687cdb9bd3ab4c365328b9b062aab3bb2"],
    "FunctionDef:_fill_evidence#1": [null,"129c721fe91f5aff0f78718072d901ecbdd58d0fef0239cc540bb3e291b11855"],
    "FunctionDef:_filter_values#1": [null,"7712f5c12e36327e83ba7fd55170a97ea870bd3a638f1fd1aba14b350f49021f"],
    "FunctionDef:_json_safe#1": [null,"d980518202371ef5ea10cc1f0a1e171e0bc64883f2b5f79fd7a014421ba9e5b4"],
    "FunctionDef:_mark_recovery#1": [null,"57d43317363dc1e4b1a7c64855b946221ec0990b5299a0d1fdc6bb88913fe5d7"],
    "FunctionDef:_normalize_quantity#1": [null,"5350fb1e07ee0bc606bc412cce2eaa138efd9179cd8b57a7ce07e1e88b20b8f9"],
    "FunctionDef:_open_orders#1": [null,"907beedbcf79faf91d2dd08d66ecd42159dd1cd0bc9ee4ba4b3adc8d8b2be40e"],
    "FunctionDef:_record#1": [null,"e5a93ca03bd81490ef44249a10702cfae9e50ae440641fee267f3a193175e9b1"],
    "FunctionDef:_restore_protection#1": [null,"a2d110ea1190488322c5a04850fc19f7c71ae1a1dee3119bc5c14e6d34216873"],
    "FunctionDef:_result#1": [null,"3fc644d4ff5c0b367de2781a2d39cac65044757cef4fd6a7c52c6e1570bda847"],
    "FunctionDef:_validate_oco#1": [null,"6e850b97dd218f35ac140318409e03f89ee13d6641bd7bb6f579998dc5ee21d7"],
    "FunctionDef:attempt_preventive_long_spot_close#1": [null,"6447e986ecd4df7e90e298f9fff400ad94ebe43d69ca7afb84a421ce99ebfec1"],
    "Import:import config#1": [null,"1dfaa2106dcbd53b68fc211b1fdf4aaad5286b2660ee0830cb825b00c00d67be"],
    "Import:import decision_timeline#1": [null,"939ce4ef5e6450229d506a4b530fa42753eaadb60bec0c210d325777c6b9d51a"],
    "Import:import hashlib#1": [null,"092cb77ae0770f1627b29bf41b2732722fbcc3a0e1d77a79674801800a2721d1"],
    "Import:import longs#1": [null,"808b346aea5beee1f1087ca2f8551132d0888d6d8882af2b5e3583a903932489"],
    "Import:import residuals#1": [null,"adbbed05f36e8bf334c4550944951b9473ed464376152819853c88b21112da54"],
    "ImportFrom:from decimal import Decimal, InvalidOperation, ROUND_DOWN#1": [null,"09f9a99873e65cbe0361e564052e2b218180472bf1b4f2deba3f70e96639ec24"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity#1": [null,"78796685355c129630527542047b70b12a3326531694998ce637553e2f7f2fe4"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/quantity_integrity.py": {
    "FunctionDef:format_decimal_quantity#1": [null,"959bbd1ca13ddb7fb74d0f3ec854988da4a06c74dcc6ee1c024775b17dd638b9"],
    "ImportFrom:from decimal import Decimal, InvalidOperation, ROUND_DOWN#1": [null,"09f9a99873e65cbe0361e564052e2b218180472bf1b4f2deba3f70e96639ec24"],
    "ImportFrom:from decimal import Decimal, ROUND_DOWN#1": ["d972b2bce6e9cc96233fedd7f7f81e56df92c264c7d3a793bdf0cad3a88a998a",null]
  },
  "trading/sl_guardian.py": {
    "FunctionDef:_close_spot_market#1": ["64007ff79c8f699117cc81825a446745b85f74691e445ba0c53bb51a24cbbbca","332e3fafb2c078be4c4c0140488146afbbc2d385b43399e4cb8a446596383f9f"],
    "FunctionDef:_run#1": ["eca5558810fd0c09504f3ad6fe2ce0fce954b2031e7003bf17a487ce3a2185bf","376b7f436e52b654d46141f6bb8c6e427f14af1dbc53fc1e772c9ecc9abee4b3"],
    "ImportFrom:from quantity_integrity import format_decimal_quantity#1": [null,"78796685355c129630527542047b70b12a3326531694998ce637553e2f7f2fe4"],
    "ImportFrom:from spot_recovery_lock import is_spot_long_recovery_pending#1": [null,"9b2640ce00115f0fe9ea2a44af561b4f9f850fa0a342c6fcd255dfc73b027600"]
  },
  "trading/spot_recovery_lock.py": {
    "Expr:'Canonical lifecycle lock for unresolved managed Spot LONG r#1": [null,"38c1a38dc0b7b62377f294c4df694ee6173393229dc4968cfe082da85022ae03"],
    "FunctionDef:is_spot_long_recovery_pending#1": [null,"6a5809269b2bafab7588f510a31386d1735344f6d7e82fbb6eab11c144f2db61"],
    "FunctionDef:spot_long_recovery_kind#1": [null,"09d8a4e0e6b7d6e2bd15893168d77a5a7ce14a91a72e673b089a22c67ec5f635"]
  },
  "trading/version_history.py": {
    "Assign:BOT_VERSION#1": ["6a7ec07ab74ff9c06ac879f1b76bbb4133b670b161b1986f860e272546d54861","79b2fcb423551f76c21f2ec2ebd8b73306a185ee5999dea89415f237e8a2a4a5"],
    "Assign:VERSION_HISTORY#1": ["7f44d963cadeb7176703626a94488fb20335045bb670cdd0d3d7332d4433bc68","4780857e643faee4f05899ab3a994186505c0292475518a40155a6d7189ab334"]
  }
}''')
_AUDITED_V16_METADATA_DELTAS = json.loads(r'''{
  "trading/capability_history.py": {
    "Assign:CAPABILITIES#1": ["547e5d072e0a7d5b47762aa50526e1f3058cb09c1825f5fbbd067b44222b2cc4","81dc4c1d90236e3728620ee93f57f392f86043bed9cacd2078a3b91e8ff70e7f"]
  },
  "trading/check_version_consistency.py": {
    "FunctionDef:validate#1": ["425d25828117571e04d8133caacf778cf865c2e763f8362a267e529972b25f25","a5abeae5c8621e1a83d852cbd151396cc0c9db4a64dbd49d9dc581831cbcfc58"]
  },
  "trading/version_history.py": {
    "Assign:BOT_VERSION#1": ["6a7ec07ab74ff9c06ac879f1b76bbb4133b670b161b1986f860e272546d54861","e1ef21fd347d69312f2fe19463c36e499cb43a07eeb6d63958588409712ec2bc"],
    "Assign:VERSION_HISTORY#1": ["7f44d963cadeb7176703626a94488fb20335045bb670cdd0d3d7332d4433bc68","e2d3db5cc9f01efea420ca934a7260afef0cf5f36e10794567b51d88f491ae32"]
  }
}''')
_AUDITED_V17_GAPS = {
    "trading/auto_loop.py": "15883623846707b0969b6b1b5b82243734171b84e8add26c09b6c63bee2ed28d",
    "trading/capability_history.py": "b9bb893d2a15caf929b4833fb48ecd228bb5233b907eb050d2d78d6ce52d8d8b",
    "trading/check_version_consistency.py": "55ea3a68dd7662f23e768bd516a9eaa7f0091fca680bb6339341305b65a3fe7b",
    "trading/entry_spot_recovery.py": "f7c9713d12529a34316bb0a87ce90fe518cafa1c25e1f920ee4da5997ac708d1",
    "trading/longs.py": "fce2815be04929b4b0d0fcb49b80d00341bb7edfb7af83aa273b8844264a5859",
    "trading/orchestration/audit_pipeline.py": "540eb0618407745f49442ad324849f5325771d7584d3055557917cbf5633cc47",
    "trading/orchestration/cycle_runner.py": "e20c11279c6583839886cca41586f69193b23ee13e20353526cb718259f57cdb",
    "trading/orchestration/position_lifecycle.py": "abe12fdf53fb762756a3b71e339f38997cf4d6ad1f137fcea734d738b0d9572b",
    "trading/partial_spot_long.py": "4f679416b0bddc14f126a11e8f7c5750bde9c44e385fa9f420f6e6397a1f0e43",
    "trading/preventive_spot_close.py": "82bb386a7c5eb7e45341a69c4061ca673cc51bf826d1b5df02a56de16262a444",
    "trading/quantity_integrity.py": "65cbd43eddf82f6295c9809d88bb480ec74b45ff367e6e7ede24666ee882b0f8",
    "trading/sl_guardian.py": "7672e02dec7a5cf286c338ee2f034afcb72c8afe02f3cf38578c3caf6c7ce0b9",
    "trading/spot_recovery_lock.py": "538d6440534fa5f615e8a26932792a82a2e4a33a97886e2d815eab8fc216d415",
    "trading/version_history.py": "d63f8b5923b2fbd2d39eb0e268ab2f0a03a320aead24f625c93a8b7ff598ae55",
}
FINAL_SPOT_CRITICAL_FILES = tuple(_AUDITED_NODE_DELTAS)
_V17_NEW_PATHS = frozenset({"trading/partial_spot_long.py", "trading/spot_recovery_lock.py", "trading/entry_spot_recovery.py", "trading/preventive_spot_close.py"})
_AUDITED_NEW_OFFLINE_TEST_PATHS = frozenset({
    "trading/test_cycle_recovery_lock.py",
    "trading/test_entry_spot_recovery.py",
    "trading/test_partial_spot_long.py",
    "trading/test_preventive_spot_close.py",
})
_V17_CONTRACT_PATHS = {
    "A_preventive_spot": ("trading/preventive_spot_close.py", "trading/orchestration/cycle_runner.py", "trading/orchestration/position_lifecycle.py"),
    "B_canonical_quantity": ("trading/quantity_integrity.py", "trading/auto_loop.py", "trading/longs.py", "trading/partial_spot_long.py", "trading/entry_spot_recovery.py", "trading/preventive_spot_close.py", "trading/sl_guardian.py"),
    "C_partial_safety": ("trading/partial_spot_long.py", "trading/quantity_integrity.py", "trading/orchestration/position_lifecycle.py"),
    "D_lifecycle_lock": ("trading/spot_recovery_lock.py", "trading/orchestration/cycle_runner.py", "trading/longs.py", "trading/preventive_spot_close.py", "trading/sl_guardian.py", "trading/orchestration/audit_pipeline.py", "trading/orchestration/position_lifecycle.py"),
    "E_entry_recovery": ("trading/entry_spot_recovery.py", "trading/spot_recovery_lock.py", "trading/longs.py", "trading/orchestration/cycle_runner.py"),
}

_AUDITED_PREVENTIVE_SPOT_AST = {
    "legacy_block": "44cb3f5cb67e53b61bb751bfb8db2d3306261428b8cad63bcc6dd1fdd9c538fe",
    "helper_wiring": "a93cfdc95cf05e4c50fdc2017e028dc62afe9b97d43ba8e36678c0cc6c3f3bb8",
    "helper_method": "7f5812b35138ee764bd2a5f44a110fac7b0af6cb588b7efc56abe2e92048b0a1",
    "helper_module": "ef9752d4f7785ebc35af7310f6acb22902604fd22db981f0a406b16d74ba0d24",
}

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_IGNORED_CYCLE_FUNCTIONS = frozenset({"sync_preventive_telegram_alert"})
_IGNORED_CYCLE_CONSTANTS = frozenset(
    {
        "PREVENTIVE_BTC_RISE_CLOSE_SHORTS_EVENT",
        "PREVENTIVE_BTC_FALL_CLOSE_LONGS_EVENT",
    }
)
_IGNORED_UTIL_FUNCTIONS = frozenset({"send_alert", "rearm_telegram_alert_event"})


class SafetyEvidenceError(ValueError):
    """Raised when read-only evidence cannot be parsed unambiguously."""


def _decimal(value, field):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise SafetyEvidenceError(f"invalid {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SafetyEvidenceError(f"invalid {field}") from exc
    if not parsed.is_finite():
        raise SafetyEvidenceError(f"invalid {field}")
    return parsed


def _order_list_id(value):
    if isinstance(value, bool):
        raise SafetyEvidenceError("invalid orderListId")
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SafetyEvidenceError("invalid orderListId") from exc
    if parsed <= 0:
        raise SafetyEvidenceError("invalid orderListId")
    return parsed


def _unsafe_record(trade_id="", symbol="", status="UNSAFE_SPOT_STATE_INCOMPLETE"):
    return {
        "trade_id": str(trade_id or ""),
        "symbol": str(symbol or "").upper(),
        "managed": False,
        "balance_aligned": False,
        "oco": False,
        "qty_aligned": False,
        "status": status,
    }


def _balance_by_asset(account):
    if not isinstance(account, dict) or not isinstance(account.get("balances"), list):
        raise SafetyEvidenceError("invalid Spot account")
    result = {}
    for row in account["balances"]:
        if not isinstance(row, dict):
            raise SafetyEvidenceError("invalid Spot balance")
        asset = str(row.get("asset") or "").strip().upper()
        if not asset or asset in result:
            raise SafetyEvidenceError("ambiguous Spot balance")
        free = _decimal(row.get("free"), "free balance")
        locked = _decimal(row.get("locked"), "locked balance")
        if free < 0 or locked < 0:
            raise SafetyEvidenceError("negative Spot balance")
        result[asset] = free + locked
    return result


def classify_spot_deploy_safety(
    local_positions,
    spot_account,
    spot_orders,
    spot_filters,
    *,
    protection_tolerance=DEFAULT_PROTECTION_TOLERANCE,
):
    """Classify local Spot positions against fresh balance and OCO evidence."""
    if not isinstance(local_positions, list):
        record = _unsafe_record(status="UNSAFE_SPOT_STATE_INCOMPLETE")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    if not isinstance(spot_orders, list):
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    if not isinstance(spot_filters, dict):
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }
    try:
        balances = _balance_by_asset(spot_account)
        tolerance = _decimal(protection_tolerance, "protection tolerance")
        if tolerance < 0:
            raise SafetyEvidenceError("negative protection tolerance")
        for order in spot_orders:
            if not isinstance(order, dict):
                raise SafetyEvidenceError("invalid Spot order")
    except SafetyEvidenceError:
        record = _unsafe_record(status="UNSAFE_SPOT_FETCH_ERROR")
        return {
            "safe": False,
            "positions_safe": False,
            "orders_safe": False,
            "records": [record],
            "unknown_orders": [],
        }

    records = []
    consumed_orders = set()
    seen_symbols = set()
    seen_order_lists = set()

    for position in local_positions:
        if not isinstance(position, dict):
            records.append(_unsafe_record(status="UNSAFE_SPOT_STATE_INCOMPLETE"))
            continue
        direction = str(position.get("direction") or "").strip().lower()
        if direction == "short":
            continue
        trade_id = str(position.get("id") or position.get("trade_id") or "").strip()
        symbol = str(position.get("symbol") or "").strip().upper()
        record = _unsafe_record(trade_id, symbol)
        if direction != "long":
            record["status"] = "UNSAFE_SPOT_UNMANAGED_POSITION"
            records.append(record)
            continue
        if not trade_id or not _SYMBOL_RE.fullmatch(symbol):
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        try:
            managed_qty = _decimal(position.get("quantity"), "managed quantity")
            local_order_list = _order_list_id(position.get("oco_order_list_id"))
        except SafetyEvidenceError:
            record["status"] = (
                "UNSAFE_SPOT_NO_OCO"
                if not str(position.get("oco_order_list_id") or "").strip()
                else "UNSAFE_SPOT_STATE_INCOMPLETE"
            )
            records.append(record)
            continue
        if managed_qty <= 0:
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        if symbol in seen_symbols or local_order_list in seen_order_lists:
            record["status"] = "UNSAFE_SPOT_STATE_INCOMPLETE"
            records.append(record)
            continue
        seen_symbols.add(symbol)
        seen_order_lists.add(local_order_list)
        record["managed"] = True

        filters = spot_filters.get(symbol)
        try:
            if not isinstance(filters, dict):
                raise SafetyEvidenceError("missing Spot filters")
            step = _decimal(filters.get("step_size"), "step size")
            if step <= 0:
                raise SafetyEvidenceError("invalid step size")
            asset = symbol[:-4]
            observed_qty = balances[asset]
        except (KeyError, SafetyEvidenceError):
            record["status"] = "UNSAFE_SPOT_FETCH_ERROR"
            records.append(record)
            continue
        balance_tolerance = max(step, MIN_QUANTITY_TOLERANCE)
        balance_delta = observed_qty - managed_qty
        if balance_delta < 0 or balance_delta > balance_tolerance:
            record["status"] = "UNSAFE_SPOT_BALANCE_MISMATCH"
            records.append(record)
            continue
        record["balance_aligned"] = True

        matching = []
        same_symbol = []
        for index, order in enumerate(spot_orders):
            order_symbol = str(order.get("symbol") or "").strip().upper()
            if order_symbol == symbol:
                same_symbol.append(index)
            try:
                observed_list = _order_list_id(order.get("orderListId"))
            except SafetyEvidenceError:
                continue
            if observed_list == local_order_list:
                matching.append(index)
        if not matching:
            record["status"] = (
                "UNSAFE_SPOT_ORDERLIST_MISMATCH" if same_symbol else "UNSAFE_SPOT_NO_OCO"
            )
            records.append(record)
            continue
        consumed_orders.update(matching)
        if len(matching) != 2:
            record["status"] = "UNSAFE_SPOT_OCO_INCOMPLETE"
            records.append(record)
            continue
        legs = [spot_orders[index] for index in matching]
        if any(str(leg.get("symbol") or "").strip().upper() != symbol for leg in legs):
            record["status"] = "UNSAFE_SPOT_SYMBOL_MISMATCH"
            records.append(record)
            continue
        if any(str(leg.get("side") or "").strip().upper() != "SELL" for leg in legs):
            record["status"] = "UNSAFE_SPOT_SIDE_MISMATCH"
            records.append(record)
            continue
        if any(str(leg.get("status") or "").strip().upper() not in ACTIVE_SPOT_ORDER_STATUSES for leg in legs):
            record["status"] = "UNSAFE_SPOT_OCO_INACTIVE"
            records.append(record)
            continue
        types = [str(leg.get("type") or "").strip().upper() for leg in legs]
        if sum(kind in SPOT_STOP_TYPES for kind in types) != 1 or sum(
            kind in SPOT_TAKE_PROFIT_TYPES for kind in types
        ) != 1:
            record["status"] = "UNSAFE_SPOT_OCO_INCOMPLETE"
            records.append(record)
            continue
        qty_tolerance = max(tolerance, managed_qty * tolerance)
        try:
            leg_quantities = [_decimal(leg.get("origQty"), "OCO quantity") for leg in legs]
        except SafetyEvidenceError:
            record["status"] = "UNSAFE_SPOT_QTY_MISMATCH"
            records.append(record)
            continue
        if any(qty <= 0 or abs(qty - managed_qty) > qty_tolerance for qty in leg_quantities):
            record["status"] = "UNSAFE_SPOT_QTY_MISMATCH"
            records.append(record)
            continue
        record.update(oco=True, qty_aligned=True, status=SAFE_MANAGED_SPOT)
        records.append(record)

    unknown_indexes = [index for index in range(len(spot_orders)) if index not in consumed_orders]
    unknown_orders = [
        {
            "symbol": str(spot_orders[index].get("symbol") or "").strip().upper(),
            "orderId": spot_orders[index].get("orderId"),
            "orderListId": spot_orders[index].get("orderListId"),
        }
        for index in unknown_indexes
    ]
    if unknown_orders:
        records.append(
            _unsafe_record(
                symbol=unknown_orders[0]["symbol"],
                status="UNSAFE_SPOT_UNKNOWN_ORDER",
            )
        )
    if not records:
        records.append(
            {
                "trade_id": "",
                "symbol": "",
                "managed": True,
                "balance_aligned": True,
                "oco": True,
                "qty_aligned": True,
                "status": SAFE_NO_SPOT,
            }
        )
    positions_safe = all(
        record["status"] in {SAFE_MANAGED_SPOT, SAFE_NO_SPOT}
        for record in records
        if record["status"] != "UNSAFE_SPOT_UNKNOWN_ORDER"
    )
    orders_safe = not unknown_orders and all(
        record["status"] in {SAFE_MANAGED_SPOT, SAFE_NO_SPOT} for record in records
    )
    return {
        "safe": positions_safe and orders_safe,
        "positions_safe": positions_safe,
        "orders_safe": orders_safe,
        "records": records,
        "unknown_orders": unknown_orders,
    }


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_ignored_cycle_call(statement):
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    function = call.func
    if isinstance(function, ast.Name) and function.id in _IGNORED_CYCLE_FUNCTIONS:
        return True
    return (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "utils"
        and function.attr == "send_alert"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "close_reason"
        and not call.keywords
    )


class _CycleAlertCallStripper(ast.NodeTransformer):
    def visit_Expr(self, node):
        if _is_ignored_cycle_call(node):
            return None
        return self.generic_visit(node)


def _ast_sha256(node):
    payload = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _statement_block_sha256(statements):
    return _ast_sha256(ast.Module(body=list(statements), type_ignores=[]))


def _single_named_member(container, node_type, name):
    matches = [
        item for item in container
        if isinstance(item, node_type) and item.name == name
    ]
    if len(matches) != 1:
        raise SafetyEvidenceError(f"expected one {name}, found {len(matches)}")
    return matches[0]


def _is_preventive_spot_import(node):
    return (
        isinstance(node, ast.Import)
        and len(node.names) == 1
        and node.names[0].name == "preventive_spot_close"
        and node.names[0].asname is None
    )


def _normalize_audited_preventive_spot_flow(tree):
    """Normalize only the exact audited legacy-to-helper transition."""
    cycle_class = _single_named_member(tree.body, ast.ClassDef, "CycleRunner")
    run_method = _single_named_member(cycle_class.body, ast.FunctionDef, "run")
    helper_methods = [
        item for item in cycle_class.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "_handle_preventive_long_spot"
    ]
    helper_imports = [item for item in tree.body if _is_preventive_spot_import(item)]
    helper_calls = [
        item for item in ast.walk(run_method)
        if isinstance(item, ast.Attribute)
        and item.attr == "_handle_preventive_long_spot"
    ]

    flow_matches = []
    for node in ast.walk(run_method):
        if not isinstance(node, ast.If) or len(node.body) < 2:
            continue
        digest = _statement_block_sha256(node.body[1:])
        if digest == _AUDITED_PREVENTIVE_SPOT_AST["legacy_block"]:
            flow_matches.append(("legacy_block", node))
        elif digest == _AUDITED_PREVENTIVE_SPOT_AST["helper_wiring"]:
            flow_matches.append(("helper_wiring", node))

    has_transition_components = bool(helper_methods or helper_imports or helper_calls)
    if not flow_matches:
        if has_transition_components:
            raise SafetyEvidenceError("incomplete preventive Spot close integration")
        return tree
    if len(flow_matches) != 1:
        raise SafetyEvidenceError("ambiguous preventive Spot close integration")

    flow_kind, flow_node = flow_matches[0]
    if flow_kind == "legacy_block":
        if has_transition_components:
            raise SafetyEvidenceError("legacy flow mixed with helper integration")
    else:
        if len(helper_imports) != 1 or len(helper_methods) != 1 or len(helper_calls) != 1:
            raise SafetyEvidenceError("incomplete preventive Spot close integration")
        if _ast_sha256(helper_methods[0]) != _AUDITED_PREVENTIVE_SPOT_AST["helper_method"]:
            raise SafetyEvidenceError("unexpected preventive Spot helper method")
        tree.body.remove(helper_imports[0])
        cycle_class.body.remove(helper_methods[0])

    flow_node.body[1:] = [
        ast.Expr(value=ast.Constant(value="AUDITED_PREVENTIVE_SPOT_CLOSE_FLOW"))
    ]
    return tree


def _normalized_module_ast(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _cycle_uses_preventive_spot_helper(root):
    path = root / "trading/orchestration/cycle_runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        _is_preventive_spot_import(item)
        or (
            isinstance(item, ast.FunctionDef)
            and item.name == "_handle_preventive_long_spot"
        )
        or (
            isinstance(item, ast.Attribute)
            and item.attr == "_handle_preventive_long_spot"
        )
        for item in ast.walk(tree)
    )


def _check_preventive_spot_helper(current_root, candidate_root, changed, errors):
    current = current_root / PREVENTIVE_SPOT_CLOSE_PATH
    candidate = candidate_root / PREVENTIVE_SPOT_CLOSE_PATH
    try:
        current_uses_helper = _cycle_uses_preventive_spot_helper(current_root)
        candidate_uses_helper = _cycle_uses_preventive_spot_helper(candidate_root)
    except (OSError, SyntaxError, UnicodeError) as exc:
        errors.append(f"parse:{PREVENTIVE_SPOT_CLOSE_PATH}:dependency:{type(exc).__name__}")
        return
    if current_uses_helper and not current.is_file():
        errors.append(f"missing:{PREVENTIVE_SPOT_CLOSE_PATH}:current")
    if candidate_uses_helper and not candidate.is_file():
        errors.append(f"missing:{PREVENTIVE_SPOT_CLOSE_PATH}:candidate")
    if not current.is_file() and not candidate.is_file():
        return
    if not candidate.is_file():
        changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
        return
    try:
        candidate_ast = _normalized_module_ast(candidate)
        if current.is_file():
            if _normalized_module_ast(current) != candidate_ast:
                changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
        elif hashlib.sha256(candidate_ast.encode("utf-8")).hexdigest() != _AUDITED_PREVENTIVE_SPOT_AST["helper_module"]:
            changed.append(PREVENTIVE_SPOT_CLOSE_PATH)
    except (OSError, SyntaxError, UnicodeError) as exc:
        errors.append(f"parse:{PREVENTIVE_SPOT_CLOSE_PATH}:{type(exc).__name__}")


def _normalized_ast(path, profile):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    normalized = copy.deepcopy(tree)
    body = []
    for node in normalized.body:
        if profile == "cycle_runner":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _IGNORED_CYCLE_FUNCTIONS:
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                if names and names <= _IGNORED_CYCLE_CONSTANTS:
                    continue
            if isinstance(node, ast.ClassDef) and node.name == "CycleRunner":
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "run":
                        stripper = _CycleAlertCallStripper()
                        member.body = [
                            stripped
                            for item in member.body
                            if (stripped := stripper.visit(item)) is not None
                        ]
        elif profile == "utils":
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _IGNORED_UTIL_FUNCTIONS:
                continue
        body.append(node)
    normalized.body = body
    if profile == "cycle_runner":
        normalized = _normalize_audited_preventive_spot_flow(normalized)
    return ast.dump(normalized, annotate_fields=True, include_attributes=False)


def _node_key(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return f"{type(node).__name__}:{node.name}"
    if isinstance(node, ast.Assign):
        return "Assign:" + ",".join(ast.unparse(target) for target in node.targets)
    if isinstance(node, ast.AnnAssign):
        return "AnnAssign:" + ast.unparse(node.target)
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return type(node).__name__ + ":" + ast.unparse(node)
    return type(node).__name__ + ":" + ast.unparse(node)[:60]


def _audited_nodes(path):
    """Expose independently checkable top-level and CycleRunner method nodes."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    nodes = {}
    counts = {}

    def add(label, node):
        counts[label] = counts.get(label, 0) + 1
        nodes[f"{label}#{counts[label]}"] = (
            _ast_sha256(node), ast.get_source_segment(source, node),
        )

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CycleRunner":
            header = ast.ClassDef(
                name=node.name, bases=node.bases, keywords=node.keywords,
                body=[ast.Pass()], decorator_list=node.decorator_list,
            )
            # Header has no source span; preserve its AST signature only.
            add("ClassHeader:CycleRunner", header)
            for member in node.body:
                add("CycleRunner." + _node_key(member), member)
        else:
            add(_node_key(node), node)
    return nodes


def _outside_node_bytes(source):
    """Keep only text outside top-level nodes and CycleRunner members."""
    tree = ast.parse(source)
    raw = source.encode("utf-8")
    lines = raw.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))

    def span(node):
        return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset

    spans = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CycleRunner":
            spans.extend(span(member) for member in node.body)
        else:
            spans.append(span(node))
    cursor = 0
    gaps = []
    for start, end in sorted(spans):
        gaps.append(raw[cursor:start])
        cursor = end
    gaps.append(raw[cursor:])
    return b"".join(gaps)


def _check_audited_nodes(relative, source, target, manifest=None):
    """Only declared node deltas may differ; all other nodes must stay equal."""
    baseline = _audited_nodes(source) if source.is_file() else {}
    candidate = _audited_nodes(target)
    allowed = (manifest or _AUDITED_NODE_DELTAS)[relative]
    for label in baseline.keys() | candidate.keys() | allowed.keys():
        before, after = allowed.get(label, (None, None))
        if label in allowed:
            if ((baseline.get(label) or (None,))[0] != before
                    or (candidate.get(label) or (None,))[0] != after):
                return False
        elif baseline.get(label) != candidate.get(label):
            return False
    if manifest is None and hashlib.sha256(
        _outside_node_bytes(target.read_text(encoding="utf-8"))
    ).hexdigest() != _AUDITED_V17_GAPS[relative]:
        return False
    return True


def _runtime_python_paths(root):
    trading = root / "trading"
    if not trading.is_dir():
        raise SafetyEvidenceError("missing trading runtime directory")
    return {
        path.relative_to(root).as_posix()
        for path in trading.rglob("*.py")
    }


def _runtime_reachable_paths(root, paths):
    """Resolve local imports from every ordinary runtime module, regardless of name."""
    modules = {
        relative.removeprefix("trading/").removesuffix(".py").replace("/", "."): relative
        for relative in paths
    }
    dependencies = {}
    for relative in paths:
        tree = ast.parse((root / relative).read_text(encoding="utf-8-sig"))
        imports = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names = [node.module]
                    names.extend(node.module + "." + alias.name for alias in node.names)
                else:
                    names = [alias.name for alias in node.names]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "__import__" and node.args
                  and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)):
                names = [node.args[0].value]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and ast.unparse(node.func) == "importlib.import_module"
                  and node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)):
                names = [node.args[0].value]
            for name in names:
                name = name.removeprefix("trading.")
                if name in modules:
                    imports.add(modules[name])
        dependencies[relative] = imports
    roots = {
        relative for relative in paths
        if not Path(relative).name.startswith("test_")
        and "testing" not in Path(relative).parts
    }
    reachable = set(roots)
    queue = list(roots)
    while queue:
        for dependency in dependencies[queue.pop()] - reachable:
            reachable.add(dependency)
            queue.append(dependency)
    return reachable


def _isolated_test_module(path):
    """Positive evidence of an offline test, never inferred from its path alone."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    imports = {
        alias.name for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names
    }
    from_imports = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom)
    }
    has_test_framework = bool({"unittest", "pytest"} & (imports | from_imports))
    has_test_cases = any(
        isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        or isinstance(node, ast.ClassDef) and any(
            ast.unparse(base) in {"unittest.TestCase", "TestCase"} for base in node.bases
        )
        for node in tree.body
    )
    return has_test_framework and has_test_cases


def _check_unrecognized_runtime_paths(current_root, candidate_root, audited, changed, errors):
    """Any new Python or changed runtime-reachable Python needs a contract."""
    try:
        old_paths = _runtime_python_paths(current_root)
        new_paths = _runtime_python_paths(candidate_root)
        reachable = (
            _runtime_reachable_paths(current_root, old_paths)
            | _runtime_reachable_paths(candidate_root, new_paths)
        )
    except (SafetyEvidenceError, OSError, SyntaxError, UnicodeError) as exc:
        errors.append(f"runtime_inventory:{exc}")
        return
    for relative in sorted((old_paths | new_paths) - set(audited)):
        current = current_root / relative
        candidate = candidate_root / relative
        # An unchanged historical test support module is nonproductive only
        # after proving no ordinary runtime import reaches it. New paths never
        # inherit this exemption from a test-like name or directory.
        if relative not in reachable and relative in old_paths and relative in new_paths:
            continue
        if (relative not in reachable and relative not in old_paths
                and relative in _AUDITED_NEW_OFFLINE_TEST_PATHS):
            try:
                if _isolated_test_module(candidate):
                    continue
            except (OSError, SyntaxError, UnicodeError) as exc:
                errors.append(f"parse:{relative}:{type(exc).__name__}")
                continue
        if not current.is_file() or not candidate.is_file():
            changed.append(relative)
            continue
        try:
            if _sha256(current) != _sha256(candidate):
                changed.append(relative)
        except OSError as exc:
            errors.append(f"read:{relative}:{type(exc).__name__}")


_V17_STRUCTURAL_REQUIREMENTS = {
    "A_preventive_spot": (
        ("trading/preventive_spot_close.py", "attempt_preventive_long_spot_close", "def", ""),
        ("trading/orchestration/cycle_runner.py", "", "import", "preventive_spot_close"),
        ("trading/orchestration/cycle_runner.py", "_handle_preventive_long_spot", "call", "preventive_spot_close.attempt_preventive_long_spot_close"),
        ("trading/orchestration/cycle_runner.py", "run", "call", "self._handle_preventive_long_spot"),
    ),
    "B_canonical_quantity": (
        ("trading/quantity_integrity.py", "format_decimal_quantity", "def", ""),
        ("trading/longs.py", "", "from", "quantity_integrity.format_decimal_quantity"),
        ("trading/auto_loop.py", "market_sell_all", "call", "format_decimal_quantity"),
        ("trading/longs.py", "open_long", "call", "format_decimal_quantity"),
        ("trading/orchestration/position_lifecycle.py", "recolocar_oco_long", "call", "format_decimal_quantity"),
        ("trading/partial_spot_long.py", "_oco_payload", "call", "format_decimal_quantity"),
        ("trading/entry_spot_recovery.py", "prepare_entry_recovery", "call", "format_decimal_quantity"),
        ("trading/preventive_spot_close.py", "attempt_preventive_long_spot_close", "call", "format_decimal_quantity"),
        ("trading/sl_guardian.py", "_close_spot_market", "call", "format_decimal_quantity"),
    ),
    "C_partial_safety": (
        ("trading/partial_spot_long.py", "attempt_partial_long_spot", "def", ""),
        ("trading/partial_spot_long.py", "reconcile_pending_partial_long_spot", "def", ""),
        ("trading/orchestration/position_lifecycle.py", "", "import", "partial_spot_long"),
        ("trading/orchestration/position_lifecycle.py", "check_partial_long", "call", "partial_spot_long.attempt_partial_long_spot"),
        ("trading/orchestration/position_lifecycle.py", "finalize_confirmed_partial_long", "def", ""),
        ("trading/orchestration/cycle_runner.py", "_reconcile_pending_spot_long", "call", "partial_spot_long.reconcile_pending_partial_long_spot"),
    ),
    "D_lifecycle_lock": (
        ("trading/spot_recovery_lock.py", "is_spot_long_recovery_pending", "def", ""),
        ("trading/orchestration/cycle_runner.py", "run", "guard", "is_spot_long_recovery_pending"),
        ("trading/longs.py", "manage_long", "guard", "is_spot_long_recovery_pending"),
        ("trading/preventive_spot_close.py", "attempt_preventive_long_spot_close", "guard", "is_spot_long_recovery_pending"),
        ("trading/sl_guardian.py", "_run", "guard", "is_spot_long_recovery_pending"),
        ("trading/orchestration/audit_pipeline.py", "reconcile_stale_spot_positions", "guard", "is_spot_long_recovery_pending"),
        ("trading/orchestration/position_lifecycle.py", "check_partial_long", "guard", "is_spot_long_recovery_pending"),
    ),
    "E_entry_recovery": (
        ("trading/entry_spot_recovery.py", "prepare_entry_recovery", "def", ""),
        ("trading/entry_spot_recovery.py", "submit_entry_emergency_sell", "def", ""),
        ("trading/entry_spot_recovery.py", "reconcile_pending_entry_spot_long", "def", ""),
        ("trading/longs.py", "", "import", "entry_spot_recovery"),
        ("trading/longs.py", "open_long", "call", "entry_spot_recovery.prepare_entry_recovery"),
        ("trading/longs.py", "open_long", "call", "entry_spot_recovery.submit_entry_emergency_sell"),
        ("trading/orchestration/cycle_runner.py", "_reconcile_pending_spot_long", "call", "entry_spot_recovery.reconcile_pending_entry_spot_long"),
    ),
}


def _structural_contract(root, requirements):
    for relative, scope, kind, needle in requirements:
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        if kind == "import":
            if not any(
                isinstance(node, ast.Import)
                and any(alias.name == needle for alias in node.names)
                for node in tree.body
            ):
                return False
            continue
        if kind == "from":
            module, imported = needle.rsplit(".", 1)
            if not any(
                isinstance(node, ast.ImportFrom) and node.module == module
                and any(alias.name == imported for alias in node.names)
                for node in tree.body
            ):
                return False
            continue
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == scope
        ]
        if len(functions) != 1:
            return False
        if kind == "call" and not any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == needle
            for node in ast.walk(functions[0])
        ):
            return False
        if kind == "guard" and not any(
            isinstance(node, ast.If) and needle in ast.unparse(node.test)
            for node in ast.walk(functions[0])
        ):
            return False
    return True


def _final_v17_compatibility(current_root, candidate_root):
    changed = []
    errors = []
    verified_paths = set()
    current_version = current_root / "VERSION"
    candidate_version = candidate_root / "VERSION"
    try:
        if current_version.read_text(encoding="utf-8").strip() != "v1.5-preventive-futures-close-fix":
            errors.append("unexpected_source_version")
        if candidate_version.read_text(encoding="utf-8").strip() != "v1.7-partial-spot-quantity-safety":
            errors.append("unexpected_candidate_version")
    except (OSError, UnicodeError) as exc:
        errors.append(f"version_evidence:{type(exc).__name__}")

    for relative in sorted(_V17_NEW_PATHS):
        if (current_root / relative).exists():
            errors.append(f"unexpected_baseline_path:{relative}")
    for relative in _AUDITED_NODE_DELTAS:
        source = current_root / relative
        target = candidate_root / relative
        if not target.is_file():
            errors.append(f"missing:{relative}:candidate")
            continue
        try:
            if _check_audited_nodes(relative, source, target):
                verified_paths.add(relative)
            else:
                changed.append(relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"parse:{relative}:candidate:{type(exc).__name__}")

    # Files not part of the audited migration keep their existing strict bytes.
    for relative in SPOT_CRITICAL_FILES:
        if relative in _AUDITED_NODE_DELTAS:
            continue
        source = current_root / relative
        target = candidate_root / relative
        if not source.is_file() or not target.is_file():
            errors.append(f"missing:{relative}")
        elif _sha256(source) != _sha256(target):
            changed.append(relative)
    _check_unrecognized_runtime_paths(
        current_root, candidate_root,
        set(_AUDITED_NODE_DELTAS) | set(SPOT_CRITICAL_FILES), changed, errors,
    )
    contracts = {}
    for name, paths in _V17_CONTRACT_PATHS.items():
        try:
            contracts[name] = (
                all(path in verified_paths for path in paths)
                and _structural_contract(candidate_root, _V17_STRUCTURAL_REQUIREMENTS[name])
            )
        except (OSError, SyntaxError, UnicodeError):
            contracts[name] = False
    if not all(contracts.values()):
        errors.append("incomplete_v17_contract")
    compatible = not changed and not errors
    return {
        "compatible": compatible,
        "status": "SPOT_RUNTIME_COMPATIBLE" if compatible else "SPOT_RUNTIME_INCOMPATIBLE",
        "changed_critical_paths": sorted(set(changed)),
        "errors": errors,
        "contracts": contracts,
        "audited_transitions": ["v1.6_preventive_spot", "v1.7_spot_quantity_and_recovery"] if compatible else [],
    }


def check_spot_runtime_compatibility(current_root, candidate_root):
    """Fail closed when open-Spot lifecycle/runtime contracts differ."""
    current_root = Path(current_root)
    candidate_root = Path(candidate_root)
    version_path = candidate_root / "VERSION"
    try:
        candidate_version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        return {
            "compatible": False, "status": "SPOT_RUNTIME_INCOMPATIBLE",
            "changed_critical_paths": [], "errors": [f"version_evidence:{type(exc).__name__}"],
        }
    final_only = _V17_NEW_PATHS - {PREVENTIVE_SPOT_CLOSE_PATH}
    if candidate_version == "v1.7-partial-spot-quantity-safety" or any(
        (candidate_root / relative).exists() for relative in final_only
    ):
        return _final_v17_compatibility(current_root, candidate_root)
    changed = []
    errors = []
    if candidate_version and candidate_version not in {
        "v1.5-preventive-futures-close-fix", "v1.6-preventive-spot-close-fix",
    }:
        errors.append("unexpected_candidate_version")
    for relative in SPOT_CRITICAL_FILES:
        current = current_root / relative
        candidate = candidate_root / relative
        if not current.is_file() or not candidate.is_file():
            errors.append(f"missing:{relative}")
            continue
        if _sha256(current) != _sha256(candidate):
            changed.append(relative)
    for relative, profile in (
        ("trading/orchestration/cycle_runner.py", "cycle_runner"),
        ("trading/utils.py", "utils"),
    ):
        current = current_root / relative
        candidate = candidate_root / relative
        if not current.is_file() or not candidate.is_file():
            errors.append(f"missing:{relative}")
            continue
        try:
            if _normalized_ast(current, profile) != _normalized_ast(candidate, profile):
                changed.append(relative)
        except (OSError, SafetyEvidenceError, SyntaxError, UnicodeError) as exc:
            errors.append(f"parse:{relative}:{type(exc).__name__}")
    _check_preventive_spot_helper(current_root, candidate_root, changed, errors)
    for relative in (_AUDITED_V16_METADATA_DELTAS if candidate_version == "v1.6-preventive-spot-close-fix" else ()):
        source = current_root / relative
        target = candidate_root / relative
        if not source.is_file() or not target.is_file():
            errors.append(f"missing:{relative}")
            continue
        try:
            if not _check_audited_nodes(
                relative, source, target, _AUDITED_V16_METADATA_DELTAS,
            ):
                changed.append(relative)
        except (OSError, SyntaxError, UnicodeError) as exc:
            errors.append(f"parse:{relative}:{type(exc).__name__}")
    _check_unrecognized_runtime_paths(
        current_root, candidate_root,
        set(SPOT_CRITICAL_FILES) | {
            "trading/orchestration/cycle_runner.py", "trading/utils.py",
            PREVENTIVE_SPOT_CLOSE_PATH,
        } | set(_AUDITED_V16_METADATA_DELTAS), changed, errors,
    )
    return {
        "compatible": not changed and not errors,
        "status": "SPOT_RUNTIME_COMPATIBLE" if not changed and not errors else "SPOT_RUNTIME_INCOMPATIBLE",
        "changed_critical_paths": sorted(set(changed)),
        "errors": errors,
    }


def evaluate_pre_cutover_safety(
    *,
    local_state,
    bot_state,
    exchange_positions,
    futures_orders,
    spot_orders,
    spot_account,
    spot_filters,
    current_root=None,
    candidate_root=None,
    protection_tolerance=DEFAULT_PROTECTION_TOLERANCE,
):
    """Combine strict Futures checks with managed/protected Spot classification."""
    positions = local_state.get("positions") if isinstance(local_state, dict) else None
    reconciliation = None
    if isinstance(bot_state, dict):
        reconciliation = (((bot_state.get("positions") or {}).get("short") or {}).get("reconciliation"))
    spot = classify_spot_deploy_safety(
        positions,
        spot_account,
        spot_orders,
        spot_filters,
        protection_tolerance=protection_tolerance,
    )
    local_futures_clear = isinstance(positions, list) and not any(
        isinstance(position, dict)
        and str(position.get("direction") or "").strip().lower() == "short"
        for position in positions
    )
    futures_known = isinstance(exchange_positions, list)
    exchange_futures_clear = futures_known
    if futures_known:
        try:
            exchange_futures_clear = all(
                isinstance(row, dict) and _decimal(row.get("positionAmt"), "Futures position amount") == 0
                for row in exchange_positions
            )
        except SafetyEvidenceError:
            exchange_futures_clear = False
            futures_known = False
    orders_known = isinstance(futures_orders, list) and isinstance(spot_orders, list)
    spot_observation_known = isinstance(spot_account, dict) and isinstance(spot_filters, dict)
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    futures_reconciliation_aligned = (
        reconciliation.get("aligned") is True and reconciliation.get("status") == "ALINEADO"
    )
    count_checks = {}
    for name in ("managed", "orphan", "unmanaged", "unprotected", "desynced"):
        value = reconciliation.get(f"{name}_count")
        count_checks[name] = isinstance(value, int) and not isinstance(value, bool) and value == 0
    local_spot_count = sum(
        1
        for position in positions or []
        if isinstance(position, dict)
        and str(position.get("direction") or "").strip().lower() == "long"
    )
    if local_spot_count:
        compatibility = (
            check_spot_runtime_compatibility(current_root, candidate_root)
            if current_root is not None and candidate_root is not None
            else {
                "compatible": False,
                "status": "SPOT_RUNTIME_INCOMPATIBLE",
                "changed_critical_paths": [],
                "errors": ["missing_runtime_roots"],
            }
        )
    else:
        compatibility = {
            "compatible": True,
            "status": "SPOT_RUNTIME_COMPATIBILITY_NOT_REQUIRED",
            "changed_critical_paths": [],
            "errors": [],
        }
    checks = {
        "local_futures_positions_clear": local_futures_clear,
        "spot_positions_deploy_safe": spot["positions_safe"],
        "spot_orders_deploy_safe": spot["orders_safe"],
        "spot_runtime_compatible": compatibility["compatible"],
        "exchange_futures_positions": futures_known and exchange_futures_clear,
        "managed_futures": count_checks["managed"],
        "orphan_futures": count_checks["orphan"],
        "unmanaged_futures": count_checks["unmanaged"],
        "unprotected_futures": count_checks["unprotected"],
        "desynced_futures": count_checks["desynced"],
        "futures_reconciliation_aligned": futures_reconciliation_aligned,
        "futures_open_orders": isinstance(futures_orders, list) and len(futures_orders) == 0,
        "order_fetch_known": orders_known,
        "spot_observation_known": spot_observation_known,
    }
    return {
        "safe": all(checks.values()),
        "checks": checks,
        "spot": spot,
        "compatibility": compatibility,
        "local_spot_count": local_spot_count,
    }
