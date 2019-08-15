import logging

from rasa.core import utils
from rasa.utils.common import class_from_module_path

logger = logging.getLogger(__name__)


class Slot(object):
    # 无名之辈FTER: 插槽，它就像对话机器人的内存，它通过键值对的形式可用来收集存储用户输入的信息(实体)或者查询数据库的数据等
    # 这是个基类，具体的不同类型的slot由子类实现
    type_name = None  # slot存储的数据类型

    def __init__(
        self, name, initial_value=None, value_reset_delay=None, auto_fill=True
    ):
        self.name = name
        self.value = initial_value
        self.initial_value = initial_value  # slot初始值， 可以用来给slot定期恢复为原始状态
        self._value_reset_delay = value_reset_delay  # slot恢复周期/轮数，
        self.auto_fill = auto_fill  # 自动填充？暂不清楚这个属性的作用

    def feature_dimensionality(self):  # 当前slot实例可抽取多少特征值
        """How many features this single slot creates.

        The dimensionality of the array returned by `as_feature` needs
        to correspond to this value."""
        return 1

    def has_features(self):  # 当前slot实例是否可抽取特征值
        """Indicate if the slot creates any features."""
        return self.feature_dimensionality() != 0

    def value_reset_delay(self):
        # 多少轮之后把slot实例的值设为初始值，如果是None,代表不会被恢复初始化，即值是“永存的”。
        """After how many turns the slot should be reset to the initial_value.

        If the delay is set to `None`, the slot will keep its value forever."""
        # TODO: FUTURE this needs to be implemented - slots are not reset yet
        return self._value_reset_delay

    def as_feature(self):
        # 从当前slot示例中抽取特征值，由于是基类，提供了一个“虚函数”接口，留给子类实现。
        raise NotImplementedError(
            "Each slot type needs to specify how its "
            "value can be converted to a feature. Slot "
            "'{}' is a generic slot that can not be used "
            "for predictions. Make sure you add this "
            "slot to your domain definition, specifying "
            "the type of the slot. If you implemented "
            "a custom slot type class, make sure to "
            "implement `.as_feature()`."
            "".format(self.name)
        )

    def reset(self):
        # slot的值恢复为初始值
        # 由tracker._reset()调用
        self.value = self.initial_value

    def __str__(self):
        return "{}({}: {})".format(self.__class__.__name__, self.name, self.value)

    def __repr__(self):
        return "<{}({}: {})>".format(self.__class__.__name__, self.name, self.value)

    @staticmethod
    def resolve_by_type(type_name):
        """Returns a slots class by its type name."""
        # 给一个slot类型名称（字符串），返回对应的slot类
        for cls in utils.all_subclasses(Slot):
            if cls.type_name == type_name:
                return cls
        try:
            # Slot子类中没有该种类的类，可以指定lookup_path去查找
            return class_from_module_path(type_name)
        except (ImportError, AttributeError):
            raise ValueError(
                "Failed to find slot type, '{}' is neither a known type nor "
                "user-defined. If you are creating your own slot type, make "
                "sure its module path is correct.".format(type_name)
            )

    def persistence_info(self): # persistence：持久
        return {
            "type": utils.module_path_from_instance(self),  # 形如【rasa.core.DataSlot】
            "initial_value": self.initial_value,
            "auto_fill": self.auto_fill,
        }


class FloatSlot(Slot):
    # 存储浮点连续值
    type_name = "float"

    def __init__(
        self,
        name,  # 继承自父类Slot
        initial_value=None, # 继承自父类Slot
        value_reset_delay=None, # 继承自父类Slot
        auto_fill=True, # 继承自父类Slot
        max_value=1.0,
        min_value=0.0,
    ):
        super(FloatSlot, self).__init__(
            name, initial_value, value_reset_delay, auto_fill
        )
        # 定义所能接收数值的范围，超出范围的值slot只存储距离最近的边界。如domain中定义温度范围是-100~100：
        # slots:
        #    temperature:
        #       type: float
        #       min_value: -100.0
        #       max_value:  100.0
        # 如果语句中检测到slot的实际值为300，那么slot只保存值100
        self.max_value = max_value
        self.min_value = min_value

        if min_value >= max_value:  # 注意：最小值和最大值不能相等！
            raise ValueError(
                "Float slot ('{}') created with an invalid range "
                "using min ({}) and max ({}) values. Make sure "
                "min is smaller than max."
                "".format(self.name, self.min_value, self.max_value)
            )
        # 浮点数类型的slot，预设了初始值，并且初始值不在接受范围内
        if initial_value is not None and not (min_value <= initial_value <= max_value):
            logger.warning(
                "Float slot ('{}') created with an initial value {}"
                "outside of configured min ({}) and max ({}) values."
                "".format(self.name, self.value, self.min_value, self.max_value)
            )

    def as_feature(self):
        # 抽取特征值
        try:
            capped_value = max(self.min_value, min(self.max_value, float(self.value))) # 抽取特征值时，越界数据取边界值
            if abs(self.max_value - self.min_value) > 0:
                # 这个条件时必然成立的，因为构造方法中，min_value >= max_value已经抛异常了
                covered_range = abs(self.max_value - self.min_value)
            else:
                # TODO：这里添加一个日志输出点，验证这个分支是否可能执行
                covered_range = 1
            return [(capped_value - self.min_value) / covered_range]
        except (TypeError, ValueError):
            return [0.0]

    def persistence_info(self):
        # 持久化消息，由调用者保存
        d = super(FloatSlot, self).persistence_info()
        d["max_value"] = self.max_value
        d["min_value"] = self.min_value
        return d


class BooleanSlot(Slot):
    # 存储布尔值，True or False；
    type_name = "bool"

    def as_feature(self):
        try:
            if self.value is not None:
                return [1.0, float(float(self.value) != 0.0)]  # 为什么是两个嘞？
            else:
                return [0.0, 0.0]
        except (TypeError, ValueError):
            # we couldn't convert the value to float - using default value
            return [0.0, 0.0]

    def feature_dimensionality(self):
        return len(self.as_feature()) # 永远是2 ？


class TextSlot(Slot):
    # 存储文本信息
    type_name = "text"

    def as_feature(self):
        return [1.0 if self.value is not None else 0.0] # 文本不空，特征值就是1


class ListSlot(Slot):
    # 无名之辈FTER: 存储列表数据，且列表的长度不影响对话
    type_name = "list"

    def as_feature(self):
        try:
            if self.value is not None and len(self.value) > 0:
                return [1.0]
            else:
                return [0.0]
        except (TypeError, ValueError):
            # we couldn't convert the value to a list - using default value
            return [0.0]


class UnfeaturizedSlot(Slot):
    # 无名之辈FTER: 用于存储不影响会话流程的数据。
    # 这个槽不会有任何的特性，因此它的值不会影响对话流，并且在预测机器人应该运行的下一个动作时被忽略。
    type_name = "unfeaturized"

    def as_feature(self):
        return []

    def feature_dimensionality(self):
        return 0


class CategoricalSlot(Slot):
    # 指定接收枚举所列的一个值， 如domain.yml中定义：
    # slots:
    #    risk_level:
    #       type: categorical
    #       values:
    #       - low
    #       - medium
    #       - high
    type_name = "categorical"

    def __init__(
        self,
        name,
        values=None,
        initial_value=None,
        value_reset_delay=None,
        auto_fill=True,
    ):
        super(CategoricalSlot, self).__init__(
            name, initial_value, value_reset_delay, auto_fill
        )
        # 可选字符串列表（小写化）如['low', 'medium', 'high']
        self.values = [str(v).lower() for v in values] if values else []

    def persistence_info(self):
        d = super(CategoricalSlot, self).persistence_info()
        d["values"] = self.values
        return d

    def as_feature(self):
        r = [0.0] * self.feature_dimensionality()  # 可选项的个数

        try:
            for i, v in enumerate(self.values):
                if v == str(self.value).lower():  # self.value是从父类继承来的，值为initial_value
                    r[i] = 1.0
                    break
            else:
                # 选中项不在可选项中
                if self.value is not None:
                    logger.warning(
                        "Categorical slot '{}' is set to a value ('{}') "
                        "that is not specified in the domain. "
                        "Value will be ignored and the slot will "
                        "behave as if no value is set. "
                        "Make sure to add all values a categorical "
                        "slot should store to the domain."
                        "".format(self.name, self.value)
                    )
        except (TypeError, ValueError):
            logger.exception("Failed to featurize categorical slot.")
            return r
        return r

    def feature_dimensionality(self):
        return len(self.values)


class DataSlot(Slot):
    def __init__(self, name, initial_value=None, value_reset_delay=1, auto_fill=True):
        super(DataSlot, self).__init__(
            name, initial_value, value_reset_delay, auto_fill
        )

    def as_feature(self):
        # TODO：日期的特征值没办法抽取吗？或者是忘了实现。
        raise NotImplementedError(
            "Each slot type needs to specify how its "
            "value can be converted to a feature."
        )
# ==========================================================================================================
# slot类型太少，不能满足需求？自定义slot:  https://rasa.com/docs/rasa/core/slots/#custom-slot-types
# ==========================================================================================================
# 如何填充slot?
#     （一）Slots Set from NLU
#           训练NLU模型时，标记了一个名为name的实体，
#           并且在Rasa Core的domain.yml文件中也包含一个具有相同名称的slot(插槽)，
#           那么当用户输入一条Message时，
#           NLU模型会对这个name进行实体提取，
#           并自动填充到这个名为name的slot中。示例如下:
              # story_01
              # * greet{"name": "老蒋"}
              #   - slot{"name": "老蒋"}
              #   - utter_greet
#           注：在上述情况下，就算我们不包含- slot{"name": "老蒋"}部分，name也会被自动填充。
#
#     （二）Slots Set By Clicking Buttons
#           前面说到，在domain.yml的templates:部分，
#           Rasa Core还支持在Text Message后添加按钮，
#           当我们点击这个按钮后，
#           Rasa Core就会向RegexInterpreter发送以/开头的Message，
#           当然对于RegexInterpreter来说，
#           NLU的输入文本的格式应该与story中的一致，
#           即/intent{entities}。
#           假设对话机器人询问是否需要查询天气信息时，
#           我们在NLU训练样本中标记一个choose意图和decision实体，
#           然后再在domain.yml中将decision标记为slot，
#           当我们点击按钮后，"好的"或“不了”会被自动填充到decision的这个slot中。
#           也就是说，当我们想Rasa Core发送"/choose{"decision": "好的"}"后，
#           会直接识别到意图choose，并提取实体decision的值。
            #  templates:
            #     utter_introduce_selfcando:
            #     - text: "我能帮你查询天气信息"
            #       buttons:
            #     	- title: "好的"
            #           payload: "/choose{"decision": "好的"}"
            #    	 	- title: "不了"
            #           payload: "/choose{"decision": "不了"}"
#
#     （三）Slots Set by Actions
#           以查询天气质量为例，先看下Rasa Core项目中domain.yml和stories.md：
            # # domain.yml
            # ...
            # actions:
            #   - action_search_weather_quality
            #
            # slots:
            #    weather_quality:
            #       type: categorical
            #       values:
            #       - 优
            #       - 良
            #       - 差
            # ...
            #
            # # stories.md
            # * greet
            #   - action_search_weather_quality
            #   - slot{"weather_quality" : "优"}
            #   - utter_answer_high
            #
            # * greet
            #   - action_search_weather_quality
            #   - slot{"weather_quality" : "中"}
            #   - utter_answer_midddle
            #
            # * greet
            #   - action_search_weather_quality
            #   - slot{"weather_quality" : "差"}
            #   - utter_answer_low
#           注：官方文档这里说，如果slot的类型是categorical时，在stories.md的故事情节中使用- slot设置值有利于提高正确action的执行率？
#
#           在自定义action中，我们先查询天气数据库，以json格式返回，然后提取出json中weather_quality字段的值填充到weather_quality slot中返回。代码如下：
#
            # from rasa_core_sdk.actions import Action
            # from rasa_core_sdk.events import SlotSet
            # import requests
            #
            # class ActionSearchWeatherQuality(Action):
            #     def name(self):
            #         return "action_search_weather_quality"
            #
            #     def run(self, dispatcher, tracker, domain):
            #         url = "http://myprofileurl.com"
            #         data = requests.get(url).json
            #         # 解析json，填充slot
            #         return [SlotSet("weather_quality", data["weather_quality"])]
# ---------------------
# 版权声明：本文为CSDN博主「无名之辈FTER」的原创文章，遵循CC 4.0 by-sa版权协议，转载请附上原文出处链接及本声明。
# 原文链接：https://blog.csdn.net/AndrExpert/article/details/92805022