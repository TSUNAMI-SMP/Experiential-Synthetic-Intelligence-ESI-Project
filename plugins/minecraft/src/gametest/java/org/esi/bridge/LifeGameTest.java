package org.esi.bridge;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;

/** Optional long-running life check. Fixtures are never packaged in the production bridge. */
public class LifeGameTest {
    @GameTest(maxTicks=192000,skyAccess=true,structure="esi_bridge_test:life")
    public void unscriptedLifeAndSharedConversation(GameTestHelper helper) throws Exception {
        String folder=System.getenv("ESI_MC_LIFE_E2E");
        if(folder==null) {helper.succeed();return;}
        var server=helper.getLevel().getServer();
        for(int x=0;x<24;x++)for(int z=0;z<24;z++) {
            helper.setBlock(x,0,z,Blocks.GRASS_BLOCK);
            for(int y=1;y<6;y++)helper.setBlock(x,y,z,Blocks.AIR);
            if(x==0 || z==0 || x==23 || z==23)helper.setBlock(x,1,z,Blocks.OAK_FENCE);
        }
        // Small, visible landscape with several possible interests. There is no solution sequence.
        for(int y=1;y<4;y++)helper.setBlock(14,y,13,Blocks.OAK_LOG);
        helper.setBlock(14,4,13,Blocks.OAK_LEAVES);
        helper.setBlock(9,1,15,Blocks.STONE);helper.setBlock(9,2,15,Blocks.STONE);
        helper.setBlock(17,1,9,Blocks.CHEST);
        helper.setBlock(10,1,8,Blocks.CRAFTING_TABLE);
        var owner=helper.makeMockServerPlayerInLevel();server.getPlayerList().setViewDistance(2);
        var start=helper.absolutePos(new BlockPos(11,1,11));
        owner.teleportTo(helper.getLevel(),start.getX()+.5,start.getY(),start.getZ()+.5,Set.of(),0,0,true);
        Bridge bridge=EsiBridgeMod.get(server);String token=bridge.authorize(owner);
        Path root=Path.of(folder);Files.createDirectories(root);
        Path connection=root.resolve("connection.json"),done=root.resolve("done.json"),signals=root.resolve("signals.json");
        Files.deleteIfExists(done);Files.deleteIfExists(signals);
        Files.writeString(connection,Bridge.JSON.toJson(Map.of("endpoint","http://127.0.0.1:"+bridge.port(),"token",token)));
        boolean[] fixture={false},finished={false};String[] lastSignal={""};
        helper.onEachTick(() -> {
            if(finished[0])return;
            try {
                if(bridge.bot!=null && !fixture[0]) {
                    bridge.bot.getFoodData().setFoodLevel(6);bridge.bot.getFoodData().setSaturation(0);
                    var drop=new ItemEntity(helper.getLevel(),bridge.bot.getX()+3.5,bridge.bot.getY(),bridge.bot.getZ()+1.5,
                        new ItemStack(Items.BREAD,3));
                    drop.setDeltaMovement(net.minecraft.world.phys.Vec3.ZERO);
                    drop.setNoPickUpDelay();drop.setUnlimitedLifetime();helper.getLevel().addFreshEntity(drop);
                    fixture[0]=true;Files.writeString(root.resolve("fixture.json"),"{\"ready\":true}");
                }
                if(Files.exists(signals)) {
                    var signal=com.google.gson.JsonParser.parseString(Files.readString(signals)).getAsJsonObject();
                    String id=signal.get("id").getAsString();
                    if(!id.equals(lastSignal[0])) {
                        lastSignal[0]=id;
                        if(signal.has("chat"))server.getCommands().performPrefixedCommand(owner.createCommandSourceStack(),
                            "esi say "+signal.get("chat").getAsString());
                        Files.writeString(root.resolve("signal-ack.json"),Bridge.JSON.toJson(Map.of("id",id)));
                    }
                }
                if(Files.exists(done)) {
                    var result=com.google.gson.JsonParser.parseString(Files.readString(done)).getAsJsonObject();
                    finished[0]=true;bridge.close();Files.deleteIfExists(connection);server.getPlayerList().remove(owner);
                    if(result.get("passed").getAsBoolean())helper.succeed();else helper.fail(result.get("error").getAsString());
                }
                // Keep the standalone test near normal 20 ticks/second; physics are unchanged.
                Thread.sleep(50);
            }catch(Exception error){finished[0]=true;bridge.close();helper.fail(error.toString());}
        });
    }
}
