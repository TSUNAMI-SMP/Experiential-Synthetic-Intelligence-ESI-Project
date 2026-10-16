package org.esi.bridge;

import carpet.fakes.ServerPlayerInterface;
import carpet.helpers.EntityPlayerActionPack;
import carpet.helpers.EntityPlayerActionPack.Action;
import carpet.helpers.EntityPlayerActionPack.ActionType;
import carpet.patches.EntityPlayerMPFake;
import carpet.patches.FakeClientConnection;
import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.mojang.authlib.GameProfile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.*;
import net.minecraft.core.BlockPos;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.PacketFlow;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ClientInformation;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.server.network.CommonListenerCookie;
import net.minecraft.resources.Identifier;
import net.minecraft.resources.ResourceKey;
import net.minecraft.util.ProblemReporter;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.GameType;
import net.minecraft.world.level.storage.LevelResource;
import net.minecraft.world.level.storage.TagValueInput;
import net.minecraft.world.phys.Vec3;

/** All world methods run on the Minecraft server thread. */
public final class Bridge implements AutoCloseable {
    static final Gson JSON = new Gson();
    final MinecraftServer server;
    private LocalApi api;
    private volatile String token = "";
    private UUID ownerId;
    EntityPlayerMPFake bot;
    private String characterId = "", lease = "", sessionId = "", worldId;
    private long heartbeat;
    private int sequence;
    private boolean paused;
    private final LinkedHashMap<String, Sense.View> views = new LinkedHashMap<>();
    private final LinkedHashMap<String, Map<String, Object>> accepted = new LinkedHashMap<>();
    private Map<String, Object> result = new LinkedHashMap<>();
    private Operation operation;
    private final Deque<Map<String,Object>> messages = new ArrayDeque<>();
    private final Set<String> spoken = new LinkedHashSet<>();
    private final Workstation workstation = new Workstation();
    public Bridge(MinecraftServer server) { this.server = server; }
    static String randomCode() {
        byte[] bytes = new byte[24]; new SecureRandom().nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }
    public String authorize(ServerPlayer owner) throws Exception {
        if (ownerId != null && !isOwner(owner) && server.getPlayerList().getPlayer(ownerId) != null)
            throw new IllegalStateException("この接続は別のプレイヤーが使用しています。");
        disconnect(); ownerId = owner.getUUID();
        if (api == null) api = new LocalApi(this);
        token = randomCode(); return token;
    }
    public int port() { return api.port(); }
    public boolean isOwner(ServerPlayer player) { return player.getUUID().equals(ownerId); }
    boolean authenticated(String header) {
        return !token.isEmpty() && header != null && MessageDigest.isEqual(
            ("Bearer "+token).getBytes(StandardCharsets.UTF_8), header.getBytes(StandardCharsets.UTF_8));
    }
    void checkLease(String value) {
        if (lease.isEmpty() || !lease.equals(value)) throw new SecurityException("接続の許可が解除されています。");
        if (ownerId == null || server.getPlayerList().getPlayer(ownerId) == null) {
            disconnect(); throw new SecurityException("接続したプレイヤーがワールドから退出しました。");
        }
        heartbeat = System.nanoTime();
    }
    public Map<String, Object> connect(JsonObject input) throws Exception {
        if (ownerId == null || server.getPlayerList().getPlayer(ownerId) == null)
            throw new IllegalStateException("先にワールド内で /esi link を実行してください。");
        if (!"minecraft.fabric".equals(text(input, "plugin_id", 80)) || number(input, "protocol", 0) != 1)
            throw new IllegalArgumentException("対応していないプラグイン接続です。");
        String id = text(input, "character_id", 128);
        if (id.isBlank()) throw new IllegalArgumentException("キャラクターIDがありません。");
        if (bot != null && !bot.isRemoved()) {
            if (!characterId.equals(id)) throw new IllegalStateException("別のESIが参加しています。先に切断してください。");
            stop("再接続した");
        } else {
            characterId = id;
            String suffix = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(id.getBytes(StandardCharsets.UTF_8))).substring(0, 10);
            String name = "ESI_"+suffix;
            UUID uuid = UUID.nameUUIDFromBytes(("OfflinePlayer:"+name).getBytes(StandardCharsets.UTF_8));
            if (server.getPlayerList().getPlayer(uuid) != null) throw new IllegalStateException("同じ名前のプレイヤーが既に参加しています。");
            ServerPlayer owner = server.getPlayerList().getPlayer(ownerId);
            GameProfile profile = new GameProfile(uuid, name);
            boolean returning = Files.exists(server.getWorldPath(LevelResource.PLAYER_DATA_DIR).resolve(uuid+".dat"));
            bot = EntityPlayerMPFake.respawnFake(server, owner.level(), profile, ClientInformation.createDefault());
            server.getPlayerList().placeNewPlayer(new FakeClientConnection(PacketFlow.SERVERBOUND), bot,
                new CommonListenerCookie(profile, 0, bot.clientInformation(), false));
            if (returning) {
                var saved = server.getPlayerList().loadPlayerData(bot.nameAndId());
                if (saved.isPresent()) {
                    try (ProblemReporter.ScopedCollector problems = new ProblemReporter.ScopedCollector(bot.problemPath(),
                            org.slf4j.LoggerFactory.getLogger("ESI Bridge"))) {
                        var inputData = TagValueInput.create(problems,bot.registryAccess(),saved.get());
                        bot.load(inputData);
                        var dimension = saved.get().getString("Dimension").orElse("minecraft:overworld");
                        var level = server.getLevel(ResourceKey.create(Registries.DIMENSION,Identifier.parse(dimension)));
                        if (level == null) throw new IllegalStateException("前回のディメンションが見つかりません。");
                        bot.teleportTo(level,bot.getX(),bot.getY(),bot.getZ(),Set.of(),bot.getYRot(),bot.getXRot(),true);
                        bot.loadAndSpawnEnderPearls(inputData); bot.loadAndSpawnParentVehicle(inputData);
                    }
                }
            }
            if (!returning) {
                Vec3 joining = owner.position();
                for (int[] offset : new int[][]{{1,0},{-1,0},{0,1},{0,-1}}) {
                    BlockPos point = owner.blockPosition().offset(offset[0],0,offset[1]);
                    if (Walking.standable(owner,point)) {
                        joining = new Vec3(point.getX()+.5,point.getY(),point.getZ()+.5); break;
                    }
                }
                bot.teleportTo(owner.level(), joining.x, joining.y, joining.z,
                    Set.of(), owner.getYRot(), owner.getXRot(), true);
            }
            bot.gameMode.changeGameModeForPlayer(GameType.SURVIVAL);
            bot.getAbilities().flying = false; bot.getAbilities().invulnerable = false;
        }
        if (worldId == null) {
            var file = server.getWorldPath(LevelResource.ROOT).resolve("data/esi-bridge-world-id.txt");
            if (Files.exists(file)) worldId = Files.readString(file).trim();
            else {
                Files.createDirectories(file.getParent()); worldId = UUID.randomUUID().toString(); Files.writeString(file, worldId);
            }
        }
        lease = randomCode(); sessionId = UUID.randomUUID().toString(); heartbeat = System.nanoTime();
        paused = false;
        views.clear(); accepted.clear(); result.clear(); messages.clear(); spoken.clear();
        return Map.of("protocol", 1, "plugin_id", "minecraft.fabric", "lease", lease,
            "bridge_version", "0.4.6", "capabilities", List.of("chat", "daylight"));
    }
    EntityPlayerActionPack pack() { return ((ServerPlayerInterface) bot).getActionPack(); }
    public Map<String, Object> state() {
        if (bot == null) throw new IllegalStateException("ESIが参加していません。");
        String id = sessionId+":"+(sequence++);
        Sense.View view = Sense.observe(bot, id, workstation); views.put(id, view);
        while (views.size() > 128) views.remove(views.keySet().iterator().next());
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("protocol", 1); value.put("id", id); value.put("plugin_id", "minecraft.fabric");
        value.put("world_id", worldId); value.put("session_id", sessionId); value.put("tick", server.getTickCount());
        value.put("actor", view.actor()); value.put("inventory", view.inventory());
        value.put("targets", view.targets()); value.put("action", new LinkedHashMap<>(result));
        value.put("recipes",view.recipes()); value.put("container",view.container());
        value.put("crafting_grid",workstation.grid(bot));
        value.put("daylight", bot.level().dimensionType().hasSkyLight()
            ? (bot.level().isBrightOutside() ? "day" : "night") : "unknown");
        value.put("messages", new ArrayList<>(messages));
        value.put("events", bot.isDeadOrDying() || bot.isRemoved() ? List.of("ESIが死亡または退出した。") : List.of());
        value.put("basis", "game_observation"); return value;
    }
    public boolean hear(ServerPlayer sender, String text) {
        if (!isOwner(sender) || bot == null || bot.isRemoved() || lease.isEmpty()) return false;
        text = text.strip();
        if (text.isEmpty() || text.length()>1000) return false;
        messages.addLast(Map.of("id",sessionId+":"+UUID.randomUUID(),"sender",sender.getUUID().toString(),
            "text",text,"tick",server.getTickCount()));
        while(messages.size()>16) messages.removeFirst();
        return true;
    }
    public Map<String,Object> say(JsonObject input) {
        String id=text(input,"id",100), message=text(input,"text",1000).strip();
        if(id.isBlank() || message.isBlank()) throw new IllegalArgumentException("会話のIDと本文が必要です。");
        var owner=server.getPlayerList().getPlayer(ownerId);
        if(owner==null || bot==null || bot.isRemoved()) throw new IllegalStateException("ESIは接続していません。");
        if(spoken.add(id)) {
            owner.sendSystemMessage(Component.literal("<ESI> "+message));
            while(spoken.size()>128) spoken.remove(spoken.iterator().next());
        }
        return Map.of("delivered",true,"id",id);
    }
    public Map<String, Object> act(JsonObject request) {
        if (paused) throw new IllegalStateException("Minecraft側で操作を停止しています。ESIで再接続してから開始してください。");
        if (bot == null || bot.isRemoved() || bot.isDeadOrDying()) throw new IllegalStateException("ESIが活動できません。状態を確認してください。");
        String requestId = text(request, "request_id", 32);
        if (!requestId.matches("[0-9a-f]{32}")) throw new IllegalArgumentException("操作のIDが正しくありません。");
        if (accepted.containsKey(requestId)) return accepted.get(requestId);
        String observationId = text(request, "observation_id", 80);
        Sense.View view = views.get(observationId);
        if (view == null || System.nanoTime()-view.created() > 120_000_000_000L)
            throw new IllegalArgumentException("観察が古くなりました。現在の周囲を確認してから操作してください。");
        if (!view.dimension().equals(bot.level().dimension().identifier().toString()))
            throw new IllegalArgumentException("観察後に別のディメンションへ移動しました。");
        String kind = text(request, "kind", 30), targetId = text(request, "target_id", 120);
        Sense.Target target = targetId.isEmpty() ? null : view.references().get(targetId);
        if (!targetId.isEmpty() && target == null) throw new IllegalArgumentException("観察にない対象は操作できません。");
        int ticks = (int) number(request, "ticks", 20);
        if (ticks < 1 || ticks > 200) throw new IllegalArgumentException("操作時間は1〜200tickです。");
        int requestedSlot = (int) bounded(request,"slot",0,0,35);
        if (operation!=null) finish("stopped","次の操作へ切り替えた",false);
        else pack().stopAll();
        if (!Set.of("craft","deposit","withdraw","close_container","look","use").contains(kind)) workstation.close(bot);
        operation = new Operation(requestId, kind, target, server.getTickCount()+ticks, bot.position(),
            bot.getFoodData().getFoodLevel(), bot.getFoodData().getSaturationLevel());
        operation.before = Sense.frame(bot,target,requestedSlot);
        result = new LinkedHashMap<>(Map.of("request_id", requestId, "kind", kind, "stage", "running",
            "changed", false, "detail", "操作を開始した", "before_observation", observationId));
        try {
            switch (kind) {
                case "look" -> {
                    float yaw=bot.getYRot(),pitch=bot.getXRot();
                    pack().look((float) bounded(request, "yaw", 0, -360, 360),
                        (float) bounded(request, "pitch", 0, -90, 90));
                    finish("completed", "視線の操作を完了した", Math.abs(yaw-bot.getYRot())>.01 || Math.abs(pitch-bot.getXRot())>.01);
                }
                case "move" -> {
                    pack().setForward((float) bounded(request, "forward", 0, -1, 1));
                    pack().setStrafing((float) bounded(request, "strafe", 0, -1, 1));
                    pack().setSprinting(flag(request, "sprint"));
                    if (flag(request, "jump")) pack().start(ActionType.JUMP, Action.continuous());
                }
                case "go_to" -> {
                    requireTarget(target); operation.path = Walking.path(bot, target.position(),target.kind().equals("block")
                        || target.kind().equals("entity") && !target.name().startsWith("item:"));
                    if (operation.path.isEmpty()) {
                        if (bot.blockPosition().distManhattan(BlockPos.containing(target.position()))<=1)
                            finish("completed", "すでに対象のすぐ近くにいる", false);
                        else throw new IllegalArgumentException("今見えている足場から安全な経路を作れません。");
                    }
                }
                case "mine" -> {
                    requireBlock(target);
                    if (bot.level().getBlockState(target.block()).getDestroySpeed(bot.level(), target.block()) < 0)
                        throw new IllegalArgumentException("このブロックは採掘できません。");
                    operation.blockName = Sense.blockName(bot.level().getBlockState(target.block()));
                    pack().lookAt(target.position()); pack().start(ActionType.ATTACK, Action.continuous());
                }
                case "eat" -> {
                    int slot = slot(request); ItemStack stack = bot.getInventory().getItem(slot);
                    if (!stack.has(DataComponents.FOOD) || !bot.getFoodData().needsFood())
                        throw new IllegalArgumentException("その所持品では今は食事できません。");
                    operation.itemName = Sense.itemName(stack); operation.count = itemCount(operation.itemName);
                    select(slot); pack().look(bot.getYRot(), -60); pack().start(ActionType.USE, Action.continuous());
                }
                case "select_slot" -> {
                    int slot=slot(request),before=bot.getInventory().getSelectedSlot();
                    select(slot);finish("completed", "持つアイテムの選択を完了した", slot>=9 || before!=slot);
                }
                case "use", "place" -> {
                    requireBlock(target); pack().lookAt(target.position());
                    if (kind.equals("place")) select(slot(request));
                    pack().start(ActionType.USE, Action.once());
                }
                case "craft" -> {
                    workstation.craft(bot,request,view);
                    finish("completed","通常のクラフトで完成品を所持品へ移した。材料と完成品は前後の所持品で確認する",true);
                }
                case "deposit", "withdraw" -> {
                    var before=Sense.inventory(bot);
                    workstation.transfer(bot,request,view,kind.equals("deposit"));
                    boolean changed=!before.equals(Sense.inventory(bot));
                    finish(changed?"completed":"failed",changed?"収納と所持品の間でアイテムを移した":"移動できなかった。空きや取り出し条件を確認する",changed);
                }
                case "close_container" -> { workstation.close(bot); finish("completed","収納・作業画面を閉じた",true); }
                case "attack" -> {
                    requireTarget(target); Entity entity = bot.level().getEntity(target.entity());
                    if (entity == null || entity instanceof ServerPlayer || entity instanceof ItemEntity
                        || entity.distanceTo(bot) > 3.5 || !bot.hasLineOfSight(entity))
                        throw new IllegalArgumentException("攻撃できる近くの対象ではありません。");
                    pack().lookAt(entity.getEyePosition()); pack().start(ActionType.ATTACK, Action.once());
                }
                case "stop" -> finish("stopped", "操作を停止した", false);
                default -> throw new IllegalArgumentException("対応していない操作です。");
            }
        } catch (RuntimeException error) { finish("failed", error.getMessage(), false); throw error; }
        Map<String, Object> response = Map.of("accepted", true, "request_id", requestId);
        accepted.put(requestId, response);
        while (accepted.size() > 256) accepted.remove(accepted.keySet().iterator().next());
        return response;
    }
    private void requireTarget(Sense.Target target) {
        if (target == null || bot.position().distanceTo(target.position()) > 12)
            throw new IllegalArgumentException("対象を近くで観察し直してください。");
    }
    private void requireBlock(Sense.Target target) {
        requireTarget(target);
        if (!target.kind().equals("block") || bot.getEyePosition().distanceTo(target.position()) > 4.5
            || !Sense.visible(bot, target.block())) throw new IllegalArgumentException("そのブロックには手が届かないか、今は見えません。");
        if (!Sense.blockName(bot.level().getBlockState(target.block())).equals(target.name()))
            throw new IllegalArgumentException("観察してからブロックが変わりました。");
    }
    private int slot(JsonObject value) {
        int slot = (int) number(value, "slot", 0);
        if (slot < 0 || slot > 35) throw new IllegalArgumentException("所持品の範囲外です。");
        return slot;
    }
    private void select(int slot) {
        if (slot >= 9) {
            ItemStack a = bot.getInventory().getItem(8), b = bot.getInventory().getItem(slot);
            bot.getInventory().setItem(8, b); bot.getInventory().setItem(slot, a); slot = 8;
        }
        pack().setSlot(slot+1); bot.getInventory().setChanged(); bot.inventoryMenu.broadcastChanges();
    }
    private int itemCount(String name) {
        int count = 0;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = bot.getInventory().getItem(slot);
            if (Sense.itemName(stack).equals(name)) count += stack.getCount();
        }
        return count;
    }
    public void tick() {
        if (lease.isEmpty()) return;
        if (server.getPlayerList().getPlayer(ownerId) == null || System.nanoTime()-heartbeat > 30_000_000_000L) {
            disconnect(); return;
        }
        if (bot == null || bot.isRemoved() || bot.isDeadOrDying()) { stop("死亡または退出したため操作を停止した"); return; }
        Operation action = operation;
        if (action == null) return;
        if (server.getTickCount() >= action.deadline) {
            boolean moved = bot.position().distanceTo(action.start) > 0.15;
            finish(action.kind.equals("move") && moved ? "completed" : "failed", "操作時間に達した", action.kind.equals("move") && moved);
            return;
        }
        switch (action.kind) {
            case "go_to" -> {
                if (action.path.isEmpty()) { finish("completed", "見えていた足場へ歩いて到着した", bot.position().distanceTo(action.start)>.15); break; }
                BlockPos point = action.path.peek();
                if (!Walking.standable(bot, point)) { finish("failed", "経路の足場が変わった", false); break; }
                Vec3 goal = new Vec3(point.getX()+.5, point.getY(), point.getZ()+.5);
                if (bot.position().distanceTo(goal) < .55) { action.path.remove(); break; }
                pack().lookAt(goal.add(0, bot.getEyeHeight(), 0)); pack().setForward(1);
                if (goal.y > bot.getY()+.3 && bot.onGround()) pack().start(ActionType.JUMP, Action.once());
            }
            case "mine" -> {
                if (!Sense.blockName(bot.level().getBlockState(action.target.block())).equals(action.blockName)) {
                    finish("completed", "対象のブロックが実際に変化した。アイテムの取得は所持品で別に確認する", true); break;
                }
                if (!Sense.visible(bot, action.target.block()) || bot.getEyePosition().distanceTo(action.target.position()) > 4.5)
                    finish("failed", "採掘中に対象が見えなくなったか離れた", false);
                else pack().lookAt(action.target.position());
            }
            case "eat" -> {
                if (itemCount(action.itemName) < action.count &&
                    (bot.getFoodData().getFoodLevel() > action.food || bot.getFoodData().getSaturationLevel() > action.saturation))
                    finish("completed", "食べ物の消費と満腹度または隠し満腹度の回復を観測した", true);
            }
            case "use", "place", "attack" -> {
                if (server.getTickCount() >= action.deadline-15) {
                    if (bot.containerMenu!=bot.inventoryMenu) workstation.opened(action.target);
                    var after=Sense.frame(bot,action.target,0);
                    boolean changed=!Objects.equals(action.before.get("target_block"),after.get("target_block"))
                        || !action.before.get("inventory").equals(after.get("inventory"))
                        || !action.before.get("container_id").equals(after.get("container_id"));
                    finish("completed", "通常の使用操作を送った。確認した変化だけを前後の観察に記録する", changed);
                }
            }
        }
    }
    private void finish(String stage, String detail, boolean changed) {
        if (bot != null && !bot.isRemoved()) {
            pack().stopAll(); bot.stopUsingItem();
            if (!stage.equals("completed")) workstation.close(bot);
            if (operation != null) result.put("evidence",Map.of("before",operation.before,"after",Sense.frame(bot,operation.target,0)));
        }
        result.put("stage", stage); result.put("detail", detail == null ? "操作を停止した" : detail);
        result.put("changed", changed); operation = null;
    }
    public void stop(String reason) {
        if (operation != null) finish("stopped", reason, false);
        else if (bot != null && !bot.isRemoved()) { pack().stopAll(); workstation.close(bot); }
    }
    public void pause() {
        stop("Minecraft側で操作を停止した"); paused = true;
        if (bot!=null && !bot.isRemoved()) workstation.close(bot);
        result.put("stage", "stopped"); result.put("paused", true);
        result.put("detail", "Minecraft側で停止中。ESIで再接続すると再開できます。");
    }
    public void disconnect() {
        stop("接続を解除した"); lease = ""; views.clear(); operation = null;
        if (bot!=null && !bot.isRemoved()) workstation.close(bot);
        if (bot != null && !bot.isRemoved()) bot.kill(Component.literal("ESI disconnected"));
        bot = null;
    }
    @Override public void close() { disconnect(); token = ""; if (api != null) api.close(); api = null; }
    static String text(JsonObject input, String key, int maximum) {
        String value = input.has(key) ? input.get(key).getAsString() : "";
        if (value.length() > maximum) throw new IllegalArgumentException("入力が長すぎます。");
        return value;
    }
    static double number(JsonObject input, String key, double fallback) {
        double value = input.has(key) ? input.get(key).getAsDouble() : fallback;
        if (!Double.isFinite(value)) throw new IllegalArgumentException("有限の数値が必要です。");
        return value;
    }
    static double bounded(JsonObject input, String key, double fallback, double min, double max) {
        double value = number(input, key, fallback);
        if (value < min || value > max) throw new IllegalArgumentException("操作値が範囲外です。");
        return value;
    }
    static boolean flag(JsonObject value, String key) { return value.has(key) && value.get(key).getAsBoolean(); }
    static final class Operation {
        final String id, kind; final Sense.Target target; final int deadline, food; final float saturation; final Vec3 start;
        Deque<BlockPos> path = new ArrayDeque<>(); String blockName, itemName; int count;
        Map<String,Object> before;
        Operation(String id, String kind, Sense.Target target, int deadline, Vec3 start, int food, float saturation) {
            this.id=id; this.kind=kind; this.target=target; this.deadline=deadline; this.start=start; this.food=food; this.saturation=saturation;
        }
    }
}
