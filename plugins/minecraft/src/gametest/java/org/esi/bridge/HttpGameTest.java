package org.esi.bridge;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Set;
import net.fabricmc.fabric.api.gametest.v1.GameTest;
import net.minecraft.core.BlockPos;
import net.minecraft.gametest.framework.GameTestHelper;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.level.block.Blocks;

/** Optional real Python + local language model exercise. Never packaged in the release MOD. */
public class HttpGameTest {
    @GameTest(maxTicks=24000,skyAccess=true)
    public void pythonClientAndModel(GameTestHelper helper) throws Exception {
        String folder=System.getenv("ESI_MC_E2E");
        if(folder==null) {helper.succeed();return;}
        var server=helper.getLevel().getServer();
        for(int x=0;x<8;x++)for(int z=0;z<8;z++)helper.setBlock(x,0,z,Blocks.STONE);
        helper.setBlock(3,1,5,Blocks.STONE);
        var owner=helper.makeMockServerPlayerInLevel();
        server.getPlayerList().setViewDistance(2);
        var start=helper.absolutePos(new BlockPos(3,1,3));
        owner.teleportTo(helper.getLevel(),start.getX()+.5,start.getY(),start.getZ()+.5,Set.of(),0,0,true);
        Bridge bridge=new Bridge(server);String token=bridge.authorize(owner);
        Path root=Path.of(folder);Files.createDirectories(root);
        Path connection=root.resolve("connection.json"), done=root.resolve("done.json");
        Files.deleteIfExists(done);
        Files.writeString(connection,Bridge.JSON.toJson(Map.of("endpoint","http://127.0.0.1:"+bridge.port(),"token",token)));
        boolean[] fixture={false},finished={false};
        helper.onEachTick(() -> {
            if(finished[0])return;
            try {
                if(bridge.bot!=null && !fixture[0]) {
                    bridge.bot.getInventory().setItem(0,new ItemStack(Items.WOODEN_PICKAXE));
                    bridge.bot.getInventory().setItem(10,new ItemStack(Items.BREAD,2));
                    bridge.bot.getFoodData().setFoodLevel(6);bridge.bot.getFoodData().setSaturation(0);fixture[0]=true;
                }
                bridge.tick();
                if(Files.exists(done)) {
                    var result=com.google.gson.JsonParser.parseString(Files.readString(done)).getAsJsonObject();
                    finished[0]=true;bridge.close();Files.deleteIfExists(connection);
                    server.getPlayerList().remove(owner);
                    if(result.get("passed").getAsBoolean()) helper.succeed();
                    else helper.fail(result.get("error").getAsString());
                }
                // GameTest advances faster than real time. Leave time for the separate Python/LLM process.
                Thread.sleep(25);
            }catch(Exception error){finished[0]=true;bridge.close();helper.fail(error.toString());}
        });
    }
}
