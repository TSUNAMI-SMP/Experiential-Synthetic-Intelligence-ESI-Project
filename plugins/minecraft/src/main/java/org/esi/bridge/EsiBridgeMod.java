package org.esi.bridge;

import java.util.IdentityHashMap;
import java.util.Map;
import com.mojang.brigadier.arguments.StringArgumentType;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.ClickEvent;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.Style;
import net.minecraft.server.MinecraftServer;

public final class EsiBridgeMod implements ModInitializer {
    private static final Map<MinecraftServer, Bridge> BRIDGES = new IdentityHashMap<>();
    public static Bridge get(MinecraftServer server) { return BRIDGES.computeIfAbsent(server, Bridge::new); }
    @Override public void onInitialize() {
        ServerMessageEvents.CHAT_MESSAGE.register((message, sender, parameters) -> {
            String text=message.signedContent();
            if(text.startsWith("@ESI ") || text.startsWith("@esi "))
                get(sender.level().getServer()).hear(sender,text.substring(5));
        });
        ServerTickEvents.END_SERVER_TICK.register(server -> {
            Bridge bridge = BRIDGES.get(server);
            if (bridge != null) bridge.tick();
        });
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            Bridge bridge = BRIDGES.remove(server);
            if (bridge != null) bridge.close();
        });
        CommandRegistrationCallback.EVENT.register((dispatcher, access, environment) -> dispatcher.register(
            Commands.literal("esi")
                .then(Commands.literal("link").executes(context -> {
                    var owner = context.getSource().getPlayerOrException();
                    if (owner instanceof carpet.patches.EntityPlayerMPFake) return 0;
                    try {
                        Bridge bridge = get(context.getSource().getServer());
                        String token = bridge.authorize(owner);
                        context.getSource().sendSuccess(() -> Component.literal(
                            "ESI接続先: http://127.0.0.1:"+bridge.port()+"\n接続コード（クリックでコピー）: "+token)
                            .setStyle(Style.EMPTY.withClickEvent(new ClickEvent.CopyToClipboard(token))), false);
                        return 1;
                    } catch (Exception exception) {
                        context.getSource().sendFailure(Component.literal("ESI接続を開始できません: "+exception.getMessage()));
                        return 0;
                    }
                }))
                .then(Commands.literal("say").then(Commands.argument("message",StringArgumentType.greedyString()).executes(context -> {
                    Bridge bridge=get(context.getSource().getServer());
                    if(!bridge.hear(context.getSource().getPlayerOrException(),StringArgumentType.getString(context,"message"))) {
                        context.getSource().sendFailure(Component.literal("先にESIを接続してください。接続した本人からのみ話しかけられます。"));
                        return 0;
                    }
                    return 1;
                })))
                .then(Commands.literal("stop").executes(context -> {
                    Bridge bridge = get(context.getSource().getServer());
                    if (!bridge.isOwner(context.getSource().getPlayerOrException())) return 0;
                    bridge.pause();
                    context.getSource().sendSuccess(() -> Component.literal("ESIの操作を停止しました。"), false);
                    return 1;
                }))
                .then(Commands.literal("disconnect").executes(context -> {
                    Bridge bridge = get(context.getSource().getServer());
                    if (!bridge.isOwner(context.getSource().getPlayerOrException())) return 0;
                    bridge.disconnect();
                    context.getSource().sendSuccess(() -> Component.literal("ESIをワールドから切断しました。"), false);
                    return 1;
                }))));
    }
}
