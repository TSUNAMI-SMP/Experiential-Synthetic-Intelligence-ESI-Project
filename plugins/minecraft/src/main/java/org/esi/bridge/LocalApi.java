package org.esi.bridge;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Loopback HTTP bridge with private owner token, per-connection lease and bounded requests. */
final class LocalApi implements AutoCloseable {
    private final Bridge bridge;
    private final HttpServer http;
    private final ExecutorService pool = new ThreadPoolExecutor(1,2,30,TimeUnit.SECONDS,
        new ArrayBlockingQueue<>(16), runnable -> {
            Thread thread=new Thread(runnable,"ESI-local-bridge");thread.setDaemon(true);return thread;
        });
    LocalApi(Bridge bridge) throws IOException {
        this.bridge=bridge;
        HttpServer chosen=null;
        for(int port=8766;port<8776;port++) {
            try { chosen=HttpServer.create(new InetSocketAddress(InetAddress.getByName("127.0.0.1"),port),8);break; }
            catch(java.net.BindException ignored) {}
        }
        if(chosen==null) throw new IOException("8766〜8775のローカル接続ポートを使用できません。");
        http=chosen;http.setExecutor(pool);http.createContext("/v1/",this::handle);http.start();
    }
    int port(){return http.getAddress().getPort();}
    private void handle(HttpExchange exchange) throws IOException {
        try {
            if(exchange.getRequestHeaders().getFirst("Origin")!=null
                || !bridge.authenticated(exchange.getRequestHeaders().getFirst("Authorization"))) {
                send(exchange,401,Map.of("error","接続コードを確認してください。"));return;
            }
            String path=exchange.getRequestURI().getPath();
            boolean read=path.equals("/v1/state");
            if(!exchange.getRequestMethod().equals(read?"GET":"POST")) {send(exchange,405,Map.of("error","対応しない要求です。"));return;}
            byte[] bytes=exchange.getRequestBody().readNBytes(65537);
            if(bytes.length>65536) {send(exchange,413,Map.of("error","入力が大きすぎます。"));return;}
            JsonObject input=bytes.length==0?new JsonObject():JsonParser.parseString(new String(bytes,StandardCharsets.UTF_8)).getAsJsonObject();
            String lease=exchange.getRequestHeaders().getFirst("X-ESI-Lease");
            CompletableFuture<Object> response=new CompletableFuture<>();AtomicBoolean expired=new AtomicBoolean();
            bridge.server.execute(() -> {
                if(expired.get())return;
                try {
                    Object value;
                    if(path.equals("/v1/connect")) value=bridge.connect(input);
                    else {
                        bridge.checkLease(lease);
                        value=switch(path) {
                            case "/v1/state" -> bridge.state();
                            case "/v1/action" -> bridge.act(input);
                            case "/v1/chat" -> bridge.say(input);
                            case "/v1/stop" -> {bridge.stop("ESIから停止を要求した");yield Map.of("stopped",true);}
                            case "/v1/disconnect" -> {bridge.disconnect();yield Map.of("disconnected",true);}
                            default -> throw new IllegalArgumentException("対応していない接続先です。");
                        };
                    }
                    response.complete(value);
                }catch(Exception exception){response.completeExceptionally(exception);}
            });
            try {send(exchange,200,response.get(5,TimeUnit.SECONDS));}
            catch(TimeoutException error){expired.set(true);send(exchange,503,Map.of("error","ワールドが停止中か、処理が混み合っています。ワールドを再開してから接続してください。"));}
            catch(ExecutionException error){
                Throwable cause=error.getCause();send(exchange,cause instanceof SecurityException?403:400,
                    Map.of("error",cause.getMessage()==null?"要求を完了できませんでした。":cause.getMessage()));
            }
        }catch(Exception error){send(exchange,400,Map.of("error","要求の形式を読み取れませんでした。"));}
        finally{exchange.close();}
    }
    private void send(HttpExchange exchange,int status,Object value) throws IOException {
        byte[] data=Bridge.JSON.toJson(value).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type","application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Cache-Control","no-store");
        exchange.sendResponseHeaders(status,data.length);exchange.getResponseBody().write(data);
    }
    @Override public void close(){http.stop(0);pool.shutdownNow();}
}
